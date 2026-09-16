// Package initcmd is `lazyaf init`: put real, random shared secrets in your
// .env - once, and then never again (the port of scripts/bootstrap_secrets.py,
// upcoming/go-cli.md §8.1).
//
// WHAT IT WILL NOT DO
//   - It never overwrites a value you already set. Re-running is a no-op.
//   - It never prints a secret (envfile.Secret cannot be printed). Run it in
//     a shared terminal; paste its output into an issue.
//   - It never touches keys it does not manage.
//   - It never generates for a key you have pointed at a file with
//     <NAME>_FILE - you already said where that secret lives.
//
// Safe to run repeatedly and concurrently: the rewrite takes a lock next to
// the file and lands through an atomic replace (envfile), so two
// simultaneous runs cannot lose each other's keys or leave a half-written
// .env behind.
//
// The command wiring (cli/internal/cmd/init.go) is L3's; Run is the entry
// point it calls and returns a *ui.Failure the root reports.
package initcmd

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/envfile"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// RootMarkers identify a LazyAF directory: a checkout, or the folder someone
// downloaded docker-compose.release.yml and .env.example into
// (bootstrap_secrets.py:44-46). Without one, init REFUSES rather than write
// a stranger's .env into a home directory (§8.1, "semantics-changed").
var RootMarkers = []string{".env.example", "docker-compose.release.yml", "docker-compose.yml"}

// lockTimeout is envfile.LockTimeout; a variable so the refusal path can be
// tested in well under 20 s.
var lockTimeout = envfile.LockTimeout

// Options are the flags of `lazyaf init`.
type Options struct {
	// Dir is where .env belongs; "" is the current directory. It must hold a
	// RootMarker unless EnvFile is given explicitly.
	Dir string
	// EnvFile overrides <Dir>/.env. Given explicitly, no marker is required:
	// the operator named the file.
	EnvFile string
	// Template overrides <Dir>/.env.example as the seed for a missing .env.
	Template string
	// Check reports what is missing and changes nothing (exit 1 if any).
	Check bool
	// Getenv is the ambient environment; nil means os.Getenv.
	Getenv func(string) string
}

// Run executes `lazyaf init`, writing its report to stdout. A nil return is
// exit 0; a *ui.Failure carries the refusal and its exit code (1 for a
// missing secret under --check, an unmarked directory, a lock timeout or a
// write failure). No secret value is ever among the bytes written to stdout
// or carried in the error.
func Run(opts Options, stdout io.Writer) error {
	getenv := opts.Getenv
	if getenv == nil {
		getenv = os.Getenv
	}
	dir := opts.Dir
	if dir == "" {
		dir = "."
	}
	dir, err := filepath.Abs(dir)
	if err != nil {
		return ui.Fail("cannot resolve the directory: "+opts.Dir, ui.WithDetail(err.Error()))
	}

	envFile := opts.EnvFile
	if envFile == "" {
		if err := requireMarker(dir); err != nil {
			return err
		}
		envFile = filepath.Join(dir, ".env")
	} else if envFile, err = filepath.Abs(envFile); err != nil {
		return ui.Fail("cannot resolve --env-file: "+opts.EnvFile, ui.WithDetail(err.Error()))
	}
	template := opts.Template
	if template == "" {
		template = filepath.Join(dir, ".env.example")
	}

	say := func(format string, args ...any) { fmt.Fprintf(stdout, format+"\n", args...) }

	created := false
	origin := ""
	var lines []string
	var newline string
	if _, statErr := os.Stat(envFile); statErr != nil {
		if !errors.Is(statErr, os.ErrNotExist) {
			return ui.Fail("cannot read "+envFile, ui.WithDetail(statErr.Error()))
		}
		if opts.Check {
			say("MISSING %s", envFile)
			return ui.Fail(fmt.Sprintf("%s does not exist", envFile),
				ui.WithRemedy("Create it, and the shared secrets, with:\n    lazyaf init"))
		}
		lines, newline, origin = seedFromTemplate(template)
		created = true
	} else {
		lines, newline, err = envfile.Load(envFile)
		if err != nil {
			return ui.Fail("cannot read "+envFile, ui.WithDetail(err.Error()))
		}
	}

	_, actions, err := envfile.Plan(lines, getenv)
	if err != nil {
		return ui.Fail("cannot generate a secret", ui.WithDetail(err.Error()))
	}
	missing := 0
	for _, a := range actions {
		if a.Kind == envfile.Generated {
			missing++
		}
	}

	if opts.Check {
		say("Checking %s", envFile)
		for _, a := range actions {
			switch a.Kind {
			case envfile.Generated:
				say("  MISSING    %s (unset, retired default, or placeholder)", a.Key)
			case envfile.Kept:
				say("  ok         %s", a.Key)
			default:
				say("  ok         %s (via %s_FILE)", a.Key, a.Key)
			}
		}
		if missing > 0 {
			return ui.Fail(fmt.Sprintf("%d secret(s) need to be set", missing),
				ui.WithRemedy("Fix with:\n    lazyaf init"))
		}
		say("")
		say("All shared secrets are set.")
		return nil
	}

	if missing == 0 && !created {
		say("%s already has every shared secret. Nothing to do.", envFile)
		describe(say, actions, getenv)
		return nil
	}

	lock, err := envfile.Lock(envFile, lockTimeout)
	if err != nil {
		var timeout *envfile.LockTimeoutError
		if errors.As(err, &timeout) {
			return ui.Fail("could not update "+envFile, ui.WithDetail(err.Error()),
				ui.WithRemedy("Wait for the other run to finish, or delete "+timeout.Path+" if you are sure nothing is running."))
		}
		return ui.Fail("could not lock "+envFile, ui.WithDetail(err.Error()))
	}
	func() {
		defer lock.Unlock()
		// Re-read INSIDE the lock: a concurrent run may have written since
		// the read above, and its keys must survive ours
		// (bootstrap_secrets.py:459-465).
		if _, statErr := os.Stat(envFile); statErr == nil {
			lines, newline, err = envfile.Load(envFile)
			if err != nil {
				return
			}
			created = false
			origin = ""
		}
		var doc []envfile.Line
		doc, actions, err = envfile.Plan(lines, getenv)
		if err != nil {
			return
		}
		err = envfile.AtomicWrite(envFile, doc, newline)
	}()
	if err != nil {
		return ui.Fail("could not write "+envFile, ui.WithDetail(err.Error()))
	}

	if origin != "" {
		say("%s %s", envFile, origin)
	}
	say("Updated %s", envFile)
	describe(say, actions, getenv)
	say("")
	say("No secret value was printed. Do not commit .env - it is gitignored.")
	if created {
		say("Now open it and add your API keys (ANTHROPIC_API_KEY / GEMINI_API_KEY).")
	}
	return nil
}

// requireMarker refuses a directory that does not look like LazyAF's.
func requireMarker(dir string) error {
	for _, marker := range RootMarkers {
		if _, err := os.Stat(filepath.Join(dir, marker)); err == nil {
			return nil
		}
	}
	return ui.Fail(
		fmt.Sprintf("this does not look like a LazyAF directory (no .env.example / docker-compose*.yml in %s)", dir),
		ui.WithRemedy("Run it in your checkout or the folder holding docker-compose.release.yml:\n"+
			"    cd <that folder> && lazyaf init\n"+
			"or name the file:\n"+
			"    lazyaf init --env-file /path/to/.env"),
	)
}

// seedFromTemplate is the content of a brand-new .env: the template when it
// exists, else a three-line header (bootstrap_secrets.py:371-386).
func seedFromTemplate(template string) (lines []string, newline, origin string) {
	raw, err := os.ReadFile(template)
	if err == nil {
		return envfile.SplitLines(string(raw)), envfile.DetectNewline(string(raw)),
			"created from " + filepath.Base(template)
	}
	return []string{
		"# LazyAF environment. Created by lazyaf init.",
		"# .env.example was not present, so this file holds only the",
		"# generated shared secrets. See QUICKSTART.md for the rest.",
	}, envfile.DetectNewline(""), "created (no .env.example to copy)"
}

// describe prints the per-key result lines. Values are never among them
// (bootstrap_secrets.py:391-412).
func describe(say func(string, ...any), actions []envfile.Action, getenv func(string) string) {
	for _, a := range actions {
		switch a.Kind {
		case envfile.Generated:
			say("  generated  %s (%d bytes of entropy, URL-safe)", a.Key, envfile.SecretBytes)
		case envfile.Kept:
			say("  kept       %s (already set - not overwritten)", a.Key)
		default:
			say("  delegated  %s (you set %s_FILE; nothing written)", a.Key, a.Key)
		}
		if strings.TrimSpace(getenv(a.Key)) != "" {
			say("             note: %s is also exported in this shell, which overrides .env for docker compose", a.Key)
		}
	}
}
