package reconcile

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// pluginSource is cli/lazyaf/cli.py's _COLLECT_PLUGIN_SOURCE (:996-1045),
// copied VERBATIM - the shipped runner_common.pytest_lazyaf plugin only
// records OUTCOMES, so it produces nothing under --collect-only; collecting
// the DECLARED set needs this collection-time hook. Stdlib + pytest only,
// so it imports in any environment that can already run the target suite.
//
//go:embed lazyaf_collect_plugin.py
var pluginSource string

// PluginSource is the embedded collector, exported for the tests that pin
// it to the Python original while both exist.
func PluginSource() string { return pluginSource }

const pluginName = "lazyaf_collect_plugin"

// PythonEnvVar names the interpreter for --from-collect when --python is
// not given (§5).
const PythonEnvVar = "LAZYAF_PYTHON"

// Interpreter is the resolved Python and where the choice came from, so the
// "Collecting:" line can say which one is running (§5, R1).
type Interpreter struct {
	// Argv is the interpreter prefix: ["python3"] or ["py", "-3"].
	Argv   []string
	Source string
}

// Prober proves a PATH candidate RUNS, not merely resolves. Why "found"
// cannot mean exec.LookPath succeeded: a stock Windows box ships the
// Microsoft Store "App Execution Alias" %LOCALAPPDATA%\Microsoft\WindowsApps\
// python3.exe, first on PATH, which exits 9009 with "Python was not found"
// - so python3 won §5's order over the working `python` and `py -3` below
// it, and a command that ran with sys.executable under the Python CLI
// (cli/lazyaf/cli.py:1089) needed LAZYAF_PYTHON on the owner's primary
// platform (verifier finding V2-2). A candidate the prober rejects is
// skipped, and the refusal names it (R1).
type Prober func(argv []string) error

// probeTimeout bounds one candidate; a stub answers in milliseconds and a
// real interpreter starts in well under a second.
const probeTimeout = 5 * time.Second

// ProbePython runs `<argv> -c "import sys"`. Only the interpreter itself is
// proven here, deliberately not `import pytest`: §5 picks the interpreter
// in a fixed order and names it, and TestFromCollect on a box whose first
// real Python lacks pytest is red by design (§10.3) - skipping past it to
// one that happens to have pytest would be guessing which suite the
// operator meant.
func ProbePython(argv []string) error {
	ctx, cancel := context.WithTimeout(context.Background(), probeTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], append(append([]string(nil), argv[1:]...), "-c", "import sys")...)
	out, err := cmd.CombinedOutput()
	if err == nil {
		return nil
	}
	if first := strings.SplitN(strings.TrimSpace(string(out)), "\n", 2)[0]; first != "" {
		return fmt.Errorf("%v: %s", err, strings.TrimRight(first, "\r"))
	}
	return err
}

// ResolvePython picks the interpreter, in the order §5 fixes:
// --python > $LAZYAF_PYTHON > $VIRTUAL_ENV > python3 > python > `py -3`
// (Windows only). The three explicit sources are taken as given - the
// operator named them, and Collect reports a non-runnable one with its
// source. The PATH candidates must pass probe (see Prober). Nothing found
// is a refusal naming every candidate and every stub it skipped.
// lookPath is exec.LookPath and probe is ProbePython; a test hands in its
// own.
func ResolvePython(explicit string, getenv func(string) string, lookPath func(string) (string, error), probe Prober) (Interpreter, error) {
	if explicit != "" {
		return Interpreter{Argv: []string{explicit}, Source: "--python"}, nil
	}
	if v := getenv(PythonEnvVar); v != "" {
		return Interpreter{Argv: []string{v}, Source: "$" + PythonEnvVar}, nil
	}
	if venv := getenv("VIRTUAL_ENV"); venv != "" {
		candidate := filepath.Join(venv, "bin", "python")
		if runtime.GOOS == "windows" {
			candidate = filepath.Join(venv, "Scripts", "python.exe")
		}
		if _, err := os.Stat(candidate); err == nil {
			return Interpreter{Argv: []string{candidate}, Source: "$VIRTUAL_ENV"}, nil
		}
	}
	type candidate struct {
		name string
		argv func(path string) []string
	}
	candidates := []candidate{
		{"python3", func(p string) []string { return []string{p} }},
		{"python", func(p string) []string { return []string{p} }},
	}
	if runtime.GOOS == "windows" {
		candidates = append(candidates, candidate{"py -3", func(p string) []string { return []string{p, "-3"} }})
	}
	var skipped []string
	for _, c := range candidates {
		path, err := lookPath(strings.Fields(c.name)[0])
		if err != nil {
			continue
		}
		argv := c.argv(path)
		if err := probe(argv); err != nil {
			skipped = append(skipped, fmt.Sprintf("%s = %s does not run: %v", c.name, path, err))
			continue
		}
		return Interpreter{Argv: argv, Source: c.name + " on PATH"}, nil
	}
	tried := "--python, $" + PythonEnvVar + ", $VIRTUAL_ENV, python3, python"
	if runtime.GOOS == "windows" {
		tried += ", py -3"
	}
	return Interpreter{}, ui.Fail(
		"no Python interpreter found for --from-collect (tried "+tried+")",
		ui.WithDetail(strings.Join(skipped, "\n")),
		ui.WithRemedy("--from-collect runs a collector INSIDE your pytest, so it needs the "+
			"interpreter your suite uses. Name it:\n\n"+
			"    lazyaf tests reconcile <repo_id> --from-collect --python /path/to/.venv/bin/python\n"+
			"or set "+PythonEnvVar+"=/path/to/python. Every other lazyaf command needs no Python."))
}

// RepoRoot walks up from start for a .git marker, falling back to start
// (cli.py:1048-1061). The file_path convention (cross-agent contract #3) is
// REPO-ROOT-relative, so collected paths must be made relative to the same
// root the pytest plugin resolves. $LAZYAF_REPO_ROOT overrides.
func RepoRoot(start string) string {
	if override := os.Getenv("LAZYAF_REPO_ROOT"); override != "" {
		return override
	}
	current, err := filepath.Abs(start)
	if err != nil {
		return start
	}
	for dir := current; ; {
		if _, err := os.Stat(filepath.Join(dir, ".git")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return current
		}
		dir = parent
	}
}

// Collect runs `pytest --collect-only` over the suite and returns the
// declared set (cli.py:1064-1125). This is the unambiguous input for
// reconcile: it sees every test the suite DECLARES, not just the ones one
// tier happened to execute. note receives the "Collecting:" line, which
// names the interpreter chosen and why.
func Collect(ctx context.Context, o Options, note func(string)) ([]Ref, error) {
	collectPath := o.CollectPath
	if collectPath == "" {
		collectPath = "."
	}
	py, err := ResolvePython(o.Python, os.Getenv, exec.LookPath, ProbePython)
	if err != nil {
		return nil, err
	}
	tmp, err := os.MkdirTemp("", "lazyaf-collect-")
	if err != nil {
		return nil, ui.Fail("could not create a temp dir for the collector", ui.WithDetail(err.Error()))
	}
	defer os.RemoveAll(tmp)
	if err := os.WriteFile(filepath.Join(tmp, pluginName+".py"), []byte(pluginSource), 0o644); err != nil {
		return nil, ui.Fail("could not write the collector plugin", ui.WithDetail(err.Error()))
	}
	outFile := filepath.Join(tmp, "refs.json")

	argv := append(append([]string(nil), py.Argv...),
		"-m", "pytest", "--collect-only", "-q", "-p", pluginName)
	argv = append(argv, o.PytestArgs...)

	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = collectPath
	env := os.Environ()
	env = append(env,
		"LAZYAF_COLLECT_OUT="+outFile,
		"LAZYAF_COLLECT_ROOT="+RepoRoot(collectPath),
		"PYTHONPATH="+strings.TrimRight(tmp+string(os.PathListSeparator)+os.Getenv("PYTHONPATH"), string(os.PathListSeparator)),
	)
	cmd.Env = env
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if note != nil {
		note(fmt.Sprintf("Collecting: %s (cwd=%s; interpreter from %s)", strings.Join(argv, " "), collectPath, py.Source))
	}
	runErr := cmd.Run()
	exitCode := 0
	if runErr != nil {
		var exitErr *exec.ExitError
		if !errors.As(runErr, &exitErr) {
			return nil, ui.Fail(fmt.Sprintf("could not run %s (from %s)", argv[0], py.Source),
				ui.WithDetail(runErr.Error()),
				ui.WithRemedy("Name the interpreter your suite uses with --python or "+PythonEnvVar+"."))
		}
		exitCode = exitErr.ExitCode()
	}

	tail := outputTail(stdout.String()+stderr.String(), 20)
	if _, err := os.Stat(outFile); err != nil {
		return nil, ui.Fail(Refusing+fmt.Sprintf("collection produced no ref set - pytest exited %d without running the collector.", exitCode),
			ui.WithDetail(tail),
			ui.WithRemedy(fmt.Sprintf("The interpreter (%s, from %s) must be able to `import pytest` and run your suite. "+
				"Pick another with --python or "+PythonEnvVar+".", argv[0], py.Source)))
	}
	if exitCode != 0 {
		// A partial collection is exactly the ambiguity this mode exists to
		// avoid: reconciling it would orphan every test in the modules that
		// failed to import.
		return nil, ui.Fail(Refusing+fmt.Sprintf("pytest --collect-only exited %d (collection errors). The declared set is incomplete.", exitCode),
			ui.WithDetail(tail),
			ui.WithRemedy("Fix the collection errors above and run it again."))
	}
	raw, err := os.ReadFile(outFile)
	if err != nil {
		return nil, ui.Fail("could not read the collector's output", ui.WithDetail(err.Error()))
	}
	var data struct {
		Refs []any `json:"refs"`
	}
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, ui.Fail("the collector wrote something that is not a refs manifest", ui.WithDetail(err.Error()))
	}
	return Normalize(data.Refs), nil
}

// outputTail is the last n lines of a subprocess' output, for a refusal's
// quotation (cli.py:1128-1133).
func outputTail(text string, n int) string {
	lines := strings.Split(strings.TrimSpace(text), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}
