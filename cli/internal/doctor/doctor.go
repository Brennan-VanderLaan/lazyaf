// Package doctor is `lazyaf doctor`: check that this machine is ready to run
// LazyAF, before you start it (the port of scripts/preflight.py,
// upcoming/go-cli.md §8.2).
//
// Every check prints one line and, when something is wrong, the exact
// command that fixes it. Nothing here changes your machine: no pulls, no
// writes, no containers (TestNoSideEffects). Docker is only ever a
// subprocess behind the Runner seam - no SDK, no socket.
//
// SECRET HYGIENE: doctor reads .env to see WHETHER a key is set and whether
// its shape is plausible. It never prints a value, never logs one, and
// never sends one anywhere (TestNoValueIsEverPrinted).
//
// Same checks as the script, same ORDER (preflight.py:672-735): compose
// file; docker CLI / daemon / compose v2; disk; env keys + shared secrets;
// ports; service images; step images; plus one new check, the backend at
// the resolved --server URL with its provenance line. Same [ OK ]/[WARN]/
// [FAIL] vocabulary, same "docker down -> stop and only check env"
// short-circuit, same exit rule: a FAIL exits 1, a WARN never does.
//
// The command wiring (cli/internal/cmd/doctor.go) is L3's; Run is the entry
// point it calls.
package doctor

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime/debug"
	"strings"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

const (
	releaseCompose = "docker-compose.release.yml"
	devCompose     = "docker-compose.yml"

	// IssuesURL is where a doctor bug goes.
	IssuesURL = "https://github.com/Brennan-VanderLaan/lazyaf/issues"
)

// status is one check's verdict (preflight.py:61-62).
type status string

const (
	statusOK   status = "ok"
	statusWarn status = "warn"
	statusFail status = "fail"
)

var marks = map[status]string{statusOK: "[ OK ]", statusWarn: "[WARN]", statusFail: "[FAIL]"}

// reporter collects verdicts and prints them as they happen
// (preflight.py:64-73). Everything doctor says goes through report, which
// is how TestNoValueIsEverPrinted can read the whole transcript.
type reporter struct {
	w       io.Writer
	results []status
}

// report prints one check result plus indented, actionable detail lines. A
// blank detail is skipped, as the script's `"" if example.exists()` idiom
// produced one.
func (r *reporter) report(s status, title string, details ...string) {
	r.results = append(r.results, s)
	fmt.Fprintf(r.w, "%s %s\n", marks[s], title)
	for _, d := range details {
		if d == "" {
			continue
		}
		for _, physical := range strings.Split(strings.TrimRight(d, "\n"), "\n") {
			fmt.Fprintf(r.w, "       %s\n", physical)
		}
	}
}

func (r *reporter) count(s status) int {
	n := 0
	for _, got := range r.results {
		if got == s {
			n++
		}
	}
	return n
}

// Options are the flags of `lazyaf doctor`, plus the seams the tests drive.
type Options struct {
	// Dev checks the build-from-source stack (docker-compose.yml) instead
	// of the release one.
	Dev bool
	// Offline skips every registry lookup and the backend probe.
	Offline bool
	// Dir is the LazyAF directory to check; "" is the current directory.
	Dir string
	// Server is the --server flag value for the backend check ("" resolves
	// $LAZYAF_SERVER then the default, exactly as every other command).
	Server string
	// DebugTrace includes the stack in the "unexpected problem" refusal.
	DebugTrace bool

	// Getenv is the ambient environment; nil means os.Getenv.
	Getenv func(string) string
	// Exec runs docker and git; nil means a real subprocess. Tests pass a
	// recorder, which is how "docker only via subprocess" and "no side
	// effects" are proven rather than asserted.
	Exec Runner
	// LookPath is shutil.which; nil means exec.LookPath.
	LookPath func(string) (string, error)
	// FreeBytes is free disk space under a path; nil means the platform
	// call (statfs / GetDiskFreeSpaceEx).
	FreeBytes func(path string) (uint64, error)
}

func (o *Options) fill() {
	if o.Getenv == nil {
		o.Getenv = os.Getenv
	}
	if o.Exec == nil {
		o.Exec = execRunner{}
	}
	if o.LookPath == nil {
		o.LookPath = exec.LookPath
	}
	if o.FreeBytes == nil {
		o.FreeBytes = freeBytes
	}
}

// Run executes `lazyaf doctor`, writing every verdict to stdout. nil is
// READY (exit 0; warnings do not fail the run); a *ui.Failure is NOT READY
// (exit 1) and names how many problems there are. A panic anywhere inside
// becomes the "unexpected problem" refusal (preflight.py:730-735): a
// doctor must never traceback.
func Run(opts Options, stdout io.Writer) (err error) {
	defer func() {
		if p := recover(); p != nil {
			detail := ""
			remedy := "This is a bug in lazyaf doctor, not a problem with your setup.\n" +
				"Please report it: " + IssuesURL
			if opts.DebugTrace {
				detail = string(debug.Stack())
			} else {
				remedy += "\nRe-run with --debug-trace to include the stack in the report."
			}
			err = ui.Fail(fmt.Sprintf("doctor hit an unexpected problem and stopped: %v", p),
				ui.WithDetail(detail), ui.WithRemedy(remedy))
		}
	}()
	opts.fill()

	dir := opts.Dir
	if dir == "" {
		dir = "."
	}
	dir, absErr := filepath.Abs(dir)
	if absErr != nil {
		return ui.Fail("cannot resolve the directory: "+opts.Dir, ui.WithDetail(absErr.Error()))
	}

	r := &reporter{w: stdout}
	mode := "release (pull images)"
	if opts.Dev {
		mode = "dev (build from source)"
	}
	fmt.Fprintf(stdout, "LazyAF doctor\n  dir:  %s\n  mode: %s\n\n", dir, mode)

	checkComposeFile(r, dir, opts.Dev)

	if checkDocker(r, opts) {
		checkDisk(r, dir, opts.FreeBytes)
		values := checkEnv(r, dir, opts)
		checkPorts(r, values, opts)
		checkServiceImages(r, values, opts)
		checkStepImages(r, values, opts)
		checkBackend(r, opts)
	} else {
		// Every remaining check needs a working docker; stop rather than
		// print a wall of consequential failures (preflight.py:700-704).
		checkEnv(r, dir, opts)
	}

	fails, warns := r.count(statusFail), r.count(statusWarn)
	fmt.Fprintln(stdout)
	if fails > 0 {
		return ui.Fail(fmt.Sprintf("NOT READY: %d problem(s) to fix, %d warning(s).", fails, warns),
			ui.WithRemedy("Each [FAIL] above says what to do."))
	}
	if warns > 0 {
		fmt.Fprintf(stdout, "READY, with %d warning(s) - read them, then:\n", warns)
	} else {
		fmt.Fprintln(stdout, "READY. Start the stack with:")
	}
	if opts.Dev {
		fmt.Fprintln(stdout, "  docker compose up -d --build")
	} else {
		fmt.Fprintf(stdout, "  docker compose -f %s pull\n  docker compose -f %s up -d\n", releaseCompose, releaseCompose)
	}
	return nil
}

// checkComposeFile (preflight.py:656-669).
func checkComposeFile(r *reporter, dir string, dev bool) bool {
	wanted := releaseCompose
	if dev {
		wanted = devCompose
	}
	if _, err := os.Stat(filepath.Join(dir, wanted)); err == nil {
		r.report(statusOK, wanted+" found")
		return true
	}
	r.report(statusFail,
		fmt.Sprintf("%s not found in %s", wanted, dir),
		"Run lazyaf doctor from a LazyAF checkout, or from the folder holding "+wanted+":",
		"  git clone https://github.com/Brennan-VanderLaan/lazyaf.git",
		"  cd lazyaf && lazyaf doctor",
		"or point it there:  lazyaf doctor --dir /path/to/lazyaf",
	)
	return false
}

// checkDocker: CLI present, daemon reachable, compose v2 available
// (preflight.py:98-135). False stops every check that needs docker.
func checkDocker(r *reporter, opts Options) bool {
	if _, err := opts.LookPath("docker"); err != nil {
		r.report(statusFail,
			"Docker CLI not found",
			"LazyAF runs entirely in containers, so this is required.",
			"Install Docker Desktop (Windows/macOS) or Docker Engine (Linux):",
			"  https://docs.docker.com/get-docker/",
		)
		return false
	}

	code, out := opts.Exec.Run(defaultTimeout, "docker", "version", "--format", "{{.Server.Version}}")
	if code != 0 {
		said := "  (no output)"
		if out != "" {
			said = "  " + firstLine(out)
		}
		r.report(statusFail,
			"Docker daemon is not responding",
			"The CLI is installed but cannot reach the engine.",
			"Start Docker Desktop, or on Linux:  sudo systemctl start docker",
			"Docker said:",
			said,
		)
		return false
	}
	r.report(statusOK, "Docker engine "+lastLine(out)+" is running")

	code, out = opts.Exec.Run(defaultTimeout, "docker", "compose", "version", "--short")
	if code != 0 {
		r.report(statusFail,
			"`docker compose` (v2) is not available",
			"The old `docker-compose` script will not do: these compose files",
			"use v2 features (profiles, service_healthy conditions).",
			"Upgrade Docker Desktop, or install the compose plugin:",
			"  https://docs.docker.com/compose/install/",
		)
		return false
	}
	r.report(statusOK, "docker compose v"+strings.TrimPrefix(lastLine(out), "v")+" available")
	return true
}

func firstLine(s string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(s), "\n")
	return strings.TrimRight(line, "\r")
}

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	return strings.TrimRight(lines[len(lines)-1], "\r")
}
