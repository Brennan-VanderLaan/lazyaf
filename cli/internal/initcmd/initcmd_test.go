package initcmd

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/envfile"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// The re-exec seam for TestConcurrentRuns (§8.1): when the test binary is
// started with LAZYAF_TEST_HELPER=init it IS `lazyaf init` for one file and
// exits, before any test runs. TestMain rather than the `-test.run=
// TestHelperProcess` variant of the idiom, stated: a TestHelperProcess that
// returns early in a normal run is a test that passes without proving
// anything, which R4 forbids; TestMain leaves no phantom PASS in the junit.
func TestMain(m *testing.M) {
	if os.Getenv("LAZYAF_TEST_HELPER") != "init" {
		os.Exit(m.Run())
	}
	err := Run(Options{
		EnvFile:  os.Getenv("LAZYAF_TEST_HELPER_ENV_FILE"),
		Template: os.Getenv("LAZYAF_TEST_HELPER_TEMPLATE"),
	}, os.Stdout)
	os.Exit(ui.Report(err))
}

var managed = []string{"LAZYAF_STEP_AUTH_SECRET", "LAZYAF_RUNNER_AUTH_SECRET"}

// noEnv is the ambient environment of every in-process test: the shell that
// runs the suite must not decide the outcome of a test about a file.
func noEnv(string) string { return "" }

// run executes init in-process with both streams captured and returns
// stdout, stderr and the exit code ui.Report would produce.
func run(t *testing.T, opts Options) (stdout, stderr string, code int) {
	t.Helper()
	if opts.Getenv == nil {
		opts.Getenv = noEnv
	}
	out, errBuf := &bytes.Buffer{}, &bytes.Buffer{}
	savedOut, savedErr := ui.Stdout, ui.Stderr
	ui.Stdout, ui.Stderr = out, errBuf
	t.Setenv("NO_COLOR", "1")
	defer func() { ui.Stdout, ui.Stderr = savedOut, savedErr }()
	code = ui.Report(Run(opts, out))
	return out.String(), errBuf.String(), code
}

func values(t *testing.T, path string) map[string]string {
	t.Helper()
	v, err := envfile.ReadFileValues(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return v
}

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func read(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_creates_env_from_the_template_when_missing
func TestCreatesEnvFromTemplate(t *testing.T) {
	dir := t.TempDir()
	envFile := filepath.Join(dir, ".env")
	template := filepath.Join(dir, ".env.example")
	write(t, template, "# LazyAF example\n# ANTHROPIC_API_KEY=\n")
	stdout, stderr, code := run(t, Options{EnvFile: envFile, Template: template})
	if code != 0 {
		t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
	}
	text := read(t, envFile)
	// The template's own content survives - this is a seed, not a replacement.
	if !strings.HasPrefix(text, "# LazyAF example\n# ANTHROPIC_API_KEY=\n") {
		t.Fatalf("template content not seeded:\n%s", text)
	}
	for _, name := range managed {
		if values(t, envFile)[name] == "" {
			t.Fatalf("%s not generated", name)
		}
	}
	if !strings.Contains(stdout, "created from .env.example") || !strings.Contains(stdout, "add your API keys") {
		t.Fatalf("stdout:\n%s", stdout)
	}
	t.Run("a marked --dir finds the template and the env file itself", func(t *testing.T) {
		dir := t.TempDir()
		write(t, filepath.Join(dir, ".env.example"), "# shipped\n")
		stdout, stderr, code := run(t, Options{Dir: dir})
		if code != 0 {
			t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
		}
		if !strings.HasPrefix(read(t, filepath.Join(dir, ".env")), "# shipped\n") {
			t.Fatal("the template next to --dir was not used")
		}
	})
	t.Run("no template means a stated header not a silent empty file", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), ".env")
		stdout, _, _ := run(t, Options{EnvFile: envFile, Template: filepath.Join(t.TempDir(), "none")})
		if !strings.Contains(read(t, envFile), "# .env.example was not present") {
			t.Fatal("the no-template header is missing")
		}
		if !strings.Contains(stdout, "created (no .env.example to copy)") {
			t.Fatalf("stdout:\n%s", stdout)
		}
	})
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_rerunning_changes_nothing
func TestIdempotent(t *testing.T) {
	envFile := filepath.Join(t.TempDir(), ".env")
	none := filepath.Join(t.TempDir(), "none")
	run(t, Options{EnvFile: envFile, Template: none})
	before := read(t, envFile)
	info, _ := os.Stat(envFile)

	stdout, stderr, code := run(t, Options{EnvFile: envFile, Template: none})
	if code != 0 {
		t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
	}
	if read(t, envFile) != before {
		t.Fatal("a re-run rewrote the file")
	}
	if after, _ := os.Stat(envFile); !after.ModTime().Equal(info.ModTime()) {
		t.Fatal("a re-run touched the file even though nothing changed")
	}
	if !strings.Contains(stdout, "kept") || !strings.Contains(stdout, "Nothing to do") {
		t.Fatalf("stdout:\n%s", stdout)
	}
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_no_generated_secret_is_ever_printed
// ::test_an_existing_secret_is_never_printed_back ::test_check_mode_does_not_print_secrets_either
func TestNeverPrintsASecret(t *testing.T) {
	// A generated value is 64 chars of this alphabet; nothing init prints
	// legitimately is.
	generatedShape := regexp.MustCompile(`[A-Za-z0-9_\-]{64}`)
	t.Run("no generated secret is ever printed", func(t *testing.T) {
		// A short directory name: t.TempDir() embeds the subtest name, which
		// is itself a 64+ char URL-safe run and would trip the shape check.
		dir, err := os.MkdirTemp("", "lz")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { os.RemoveAll(dir) })
		envFile := filepath.Join(dir, ".env")
		stdout, stderr, _ := run(t, Options{EnvFile: envFile, Template: filepath.Join(dir, "none")})
		output := stdout + stderr
		for key, value := range values(t, envFile) {
			if strings.Contains(output, value) {
				t.Fatalf("the generated %s reached the output:\n%s", key, output)
			}
		}
		if generatedShape.MatchString(output) {
			t.Fatalf("a 64-char URL-safe run reached the output:\n%s", output)
		}
	})
	t.Run("an existing secret is never printed back", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), ".env")
		const sentinel = "SENTINEL-EXISTING-SECRET-DO-NOT-ECHO-0123456789"
		write(t, envFile, "LAZYAF_STEP_AUTH_SECRET="+sentinel+"\n")
		stdout, stderr, _ := run(t, Options{EnvFile: envFile})
		if strings.Contains(stdout+stderr, sentinel) {
			t.Fatalf("existing value echoed:\n%s%s", stdout, stderr)
		}
	})
	t.Run("check mode does not print secrets either", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), ".env")
		const sentinel = "SENTINEL-CHECK-MODE-DO-NOT-ECHO-0123456789abcd"
		write(t, envFile, "LAZYAF_STEP_AUTH_SECRET="+sentinel+"\nLAZYAF_RUNNER_AUTH_SECRET="+sentinel+"-r\n")
		stdout, stderr, code := run(t, Options{EnvFile: envFile, Check: true})
		if code != 0 {
			t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
		}
		if strings.Contains(stdout+stderr, sentinel) {
			t.Fatalf("check mode echoed a value:\n%s%s", stdout, stderr)
		}
	})
	t.Run("a placeholder being replaced is not echoed", func(t *testing.T) {
		// The retired default is public, but the habit is the point: nothing
		// read from the file reaches the output.
		envFile := filepath.Join(t.TempDir(), ".env")
		write(t, envFile, "LAZYAF_STEP_AUTH_SECRET="+envfile.RetiredPublicSecrets[0]+"\n")
		stdout, stderr, _ := run(t, Options{EnvFile: envFile})
		if strings.Contains(stdout+stderr, envfile.RetiredPublicSecrets[0]) {
			t.Fatalf("the old value was echoed:\n%s%s", stdout, stderr)
		}
	})
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_check_reports_missing_and_changes_nothing
// ::test_check_passes_once_bootstrapped ::test_check_on_a_missing_file_fails_without_creating_it
func TestCheckMode(t *testing.T) {
	t.Run("check reports missing and changes nothing", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), ".env")
		write(t, envFile, "CLAUDE_RUNNERS=4\n")
		before := read(t, envFile)
		stdout, stderr, code := run(t, Options{EnvFile: envFile, Check: true})
		if code != 1 {
			t.Fatalf("exit %d, want 1:\n%s%s", code, stdout, stderr)
		}
		if read(t, envFile) != before {
			t.Fatal("--check changed the file")
		}
		for _, name := range managed {
			if !strings.Contains(stdout, "MISSING    "+name) {
				t.Fatalf("stdout lacks MISSING %s:\n%s", name, stdout)
			}
		}
		if !strings.Contains(stderr, "lazyaf init") {
			t.Fatalf("the remedy must name the command:\n%s", stderr)
		}
	})
	t.Run("check passes once bootstrapped and reports each verdict", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), ".env")
		write(t, envFile, "LAZYAF_RUNNER_AUTH_SECRET_FILE=/run/secrets/runner\n")
		run(t, Options{EnvFile: envFile, Template: filepath.Join(t.TempDir(), "none")})
		stdout, stderr, code := run(t, Options{EnvFile: envFile, Check: true})
		if code != 0 {
			t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
		}
		if !strings.Contains(stdout, "ok         LAZYAF_STEP_AUTH_SECRET\n") {
			t.Fatalf("stdout:\n%s", stdout)
		}
		if !strings.Contains(stdout, "ok         LAZYAF_RUNNER_AUTH_SECRET (via LAZYAF_RUNNER_AUTH_SECRET_FILE)") {
			t.Fatalf("stdout:\n%s", stdout)
		}
		if !strings.Contains(stdout, "All shared secrets are set.") {
			t.Fatalf("stdout:\n%s", stdout)
		}
	})
	t.Run("check on a missing file fails without creating it", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), ".env")
		stdout, _, code := run(t, Options{EnvFile: envFile, Check: true})
		if code != 1 {
			t.Fatalf("exit %d, want 1", code)
		}
		if _, err := os.Stat(envFile); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("--check created the file")
		}
		if !strings.Contains(stdout, "MISSING "+envFile) {
			t.Fatalf("stdout:\n%s", stdout)
		}
	})
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_concurrent_runs_leave_one_consistent_file
//
// Read-modify-write on a shared file is a lost-update bug by default. Six
// REAL processes race on one fresh .env - goroutines would not exercise
// O_EXCL across processes, which is the point. The invariant is not
// "whoever wins": it is that the file that survives is well-formed and
// holds BOTH keys, and that no temp or lock file is left lying around.
func TestConcurrentRuns(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	envFile := filepath.Join(dir, ".env")
	template := filepath.Join(dir, "none")

	env := []string{
		"LAZYAF_TEST_HELPER=init",
		"LAZYAF_TEST_HELPER_ENV_FILE=" + envFile,
		"LAZYAF_TEST_HELPER_TEMPLATE=" + template,
		"NO_COLOR=1",
	}
	// The ambient shell must not decide the outcome of a test about a file.
	for _, kv := range os.Environ() {
		key, _, _ := strings.Cut(kv, "=")
		if strings.HasPrefix(key, "LAZYAF_TEST_HELPER") || key == "NO_COLOR" {
			continue
		}
		if strings.HasPrefix(key, "LAZYAF_STEP_AUTH_SECRET") || strings.HasPrefix(key, "LAZYAF_RUNNER_AUTH_SECRET") {
			continue
		}
		env = append(env, kv)
	}

	const runs = 6
	type result struct {
		out  string
		err  error
		code int
	}
	results := make([]result, runs)
	var wg sync.WaitGroup
	for i := 0; i < runs; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			cmd := exec.Command(self)
			cmd.Env = env
			out, err := cmd.CombinedOutput()
			code := 0
			var exit *exec.ExitError
			if errors.As(err, &exit) {
				code = exit.ExitCode()
				err = nil
			}
			results[i] = result{string(out), err, code}
		}(i)
	}
	wg.Wait()

	for i, r := range results {
		if r.err != nil {
			t.Fatalf("process %d could not run: %v", i, r.err)
		}
		if r.code != 0 {
			t.Errorf("process %d exited %d:\n%s", i, r.code, r.out)
		}
		// Proof the child ran init and not, say, an empty test binary.
		if !strings.Contains(r.out, "Updated "+envFile) && !strings.Contains(r.out, "already has every shared secret") {
			t.Errorf("process %d did not run init:\n%s", i, r.out)
		}
	}
	updated := 0
	for _, r := range results {
		if strings.Contains(r.out, "Updated "+envFile) {
			updated++
		}
	}
	if updated == 0 {
		t.Fatal("no process wrote the file")
	}
	got := values(t, envFile)
	for _, name := range managed {
		if len(got[name]) < 43 {
			t.Errorf("%s lost in the race (%d chars)", name, len(got[name]))
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var leftovers []string
	for _, e := range entries {
		if e.Name() != ".env" {
			leftovers = append(leftovers, e.Name())
		}
	}
	if len(leftovers) != 0 {
		t.Fatalf("temp/lock files left behind: %v", leftovers)
	}
	// Well-formed: exactly one active assignment per managed key.
	text := read(t, envFile)
	for _, name := range managed {
		if n := strings.Count(text, name+"="); n != 1 {
			t.Errorf("%d assignments of %s, want 1:\n%s", n, name, text)
		}
	}
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_a_concurrent_run_does_not_clobber_a_value_written_between_read_and_write
func TestReReadInsideTheLock(t *testing.T) {
	t.Run("a key written by the other process survives", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), ".env")
		write(t, envFile, "LAZYAF_STEP_AUTH_SECRET=written-by-the-other-process-9f2a11c4\n")
		run(t, Options{EnvFile: envFile})
		got := values(t, envFile)
		if got["LAZYAF_STEP_AUTH_SECRET"] != "written-by-the-other-process-9f2a11c4" {
			t.Fatalf("clobbered: %q", got["LAZYAF_STEP_AUTH_SECRET"])
		}
		if len(got["LAZYAF_RUNNER_AUTH_SECRET"]) < 43 {
			t.Fatal("the other key was not added")
		}
	})
	t.Run("a file that appears between the first read and the lock is honoured", func(t *testing.T) {
		// The first read finds nothing; by the time the lock is taken, the
		// "other process" (the ambient env hook here) has written one key.
		// Getenv is consulted inside the lock too, which is what proves the
		// plan is recomputed there rather than replayed from before it.
		dir := t.TempDir()
		envFile := filepath.Join(dir, ".env")
		calls := 0
		getenv := func(key string) string {
			calls++
			if calls == 1 {
				write(t, envFile, "LAZYAF_STEP_AUTH_SECRET=arrived-late-but-first-0123456789\n")
			}
			return ""
		}
		stdout, stderr, code := run(t, Options{EnvFile: envFile, Template: filepath.Join(dir, "none"), Getenv: getenv})
		if code != 0 {
			t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
		}
		got := values(t, envFile)
		if got["LAZYAF_STEP_AUTH_SECRET"] != "arrived-late-but-first-0123456789" {
			t.Fatalf("the value written before the lock was clobbered: %q", got["LAZYAF_STEP_AUTH_SECRET"])
		}
		if len(got["LAZYAF_RUNNER_AUTH_SECRET"]) < 43 {
			t.Fatal("the missing key was not added")
		}
		if strings.Contains(stdout, "created") {
			t.Fatalf("init claimed to have created a file another run created:\n%s", stdout)
		}
	})
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_default_env_file_is_the_repo_root_in_a_checkout
func TestRootMarkers(t *testing.T) {
	for _, marker := range RootMarkers {
		t.Run(marker, func(t *testing.T) {
			dir := t.TempDir()
			write(t, filepath.Join(dir, marker), "")
			stdout, stderr, code := run(t, Options{Dir: dir, Template: filepath.Join(dir, "none")})
			if code != 0 {
				t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
			}
			if _, err := os.Stat(filepath.Join(dir, ".env")); err != nil {
				t.Fatalf(".env not written beside %s: %v", marker, err)
			}
		})
	}
	t.Run("the default dir is the working directory", func(t *testing.T) {
		dir := t.TempDir()
		write(t, filepath.Join(dir, ".env.example"), "# x\n")
		t.Chdir(dir)
		stdout, stderr, code := run(t, Options{})
		if code != 0 {
			t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
		}
		if _, err := os.Stat(filepath.Join(dir, ".env")); err != nil {
			t.Fatalf(".env not written in the cwd: %v", err)
		}
	})
}

// §8.1 replacement for test_standalone_download_writes_beside_itself_not_a_level_up:
// the binary lives in ~/.local/bin, so "beside itself" is meaningless;
// without a marker init REFUSES rather than guess.
func TestRefusesUnmarkedDirectory(t *testing.T) {
	dir := t.TempDir()
	stdout, stderr, code := run(t, Options{Dir: dir})
	if code != 1 {
		t.Fatalf("exit %d, want 1:\n%s%s", code, stdout, stderr)
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Fatalf("something was written into an unmarked directory: %v", entries)
	}
	for _, want := range []string{"does not look like a LazyAF directory", ".env.example", "docker-compose.release.yml", "--env-file", dir} {
		if !strings.Contains(stderr, want) {
			t.Fatalf("refusal lacks %q:\n%s", want, stderr)
		}
	}
	t.Run("an explicit --env-file needs no marker", func(t *testing.T) {
		envFile := filepath.Join(t.TempDir(), "custom.env")
		_, stderr, code := run(t, Options{Dir: t.TempDir(), EnvFile: envFile, Template: filepath.Join(t.TempDir(), "none")})
		if code != 0 {
			t.Fatalf("exit %d:\n%s", code, stderr)
		}
	})
}

// bootstrap_secrets.py:193-199: a CRLF .env stays CRLF through init.
func TestCRLFPreservedThroughInit(t *testing.T) {
	envFile := filepath.Join(t.TempDir(), ".env")
	write(t, envFile, "# notes\r\nCLAUDE_RUNNERS=4\r\n")
	if _, stderr, code := run(t, Options{EnvFile: envFile}); code != 0 {
		t.Fatalf("exit %d:\n%s", code, stderr)
	}
	text := read(t, envFile)
	if strings.Count(text, "\r\n") != strings.Count(text, "\n") {
		t.Fatalf("a bare LF crept in:\n%q", text)
	}
	if !strings.HasPrefix(text, "# notes\r\nCLAUDE_RUNNERS=4\r\n") {
		t.Fatalf("original lines altered:\n%q", text)
	}
}

// The ambient-shell note (bootstrap_secrets.py:407-411): a value exported in
// the shell overrides .env for compose, and the operator should know.
func TestAmbientExportIsNoted(t *testing.T) {
	envFile := filepath.Join(t.TempDir(), ".env")
	getenv := func(key string) string {
		if key == "LAZYAF_STEP_AUTH_SECRET" {
			return "exported-in-the-shell-value-0123456789"
		}
		return ""
	}
	stdout, _, _ := run(t, Options{EnvFile: envFile, Template: filepath.Join(t.TempDir(), "none"), Getenv: getenv})
	if !strings.Contains(stdout, "LAZYAF_STEP_AUTH_SECRET is also exported in this shell") {
		t.Fatalf("stdout:\n%s", stdout)
	}
	if strings.Contains(stdout, "exported-in-the-shell-value") {
		t.Fatalf("the exported value was echoed:\n%s", stdout)
	}
	if strings.Contains(stdout, "LAZYAF_RUNNER_AUTH_SECRET is also exported") {
		t.Fatalf("a note for a key that is not exported:\n%s", stdout)
	}
}

// A held lock is a refusal that names the lock file, not a hang or a
// traceback (bootstrap_secrets.py:466-468).
func TestLockTimeoutIsARefusal(t *testing.T) {
	envFile := filepath.Join(t.TempDir(), ".env")
	write(t, envFile, "CLAUDE_RUNNERS=4\n")
	held, err := envfile.Lock(envFile, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Unlock()
	saved := lockTimeout
	lockTimeout = 200 * time.Millisecond
	defer func() { lockTimeout = saved }()

	start := time.Now()
	stdout, stderr, code := run(t, Options{EnvFile: envFile})
	if code != 1 {
		t.Fatalf("exit %d, want 1:\n%s%s", code, stdout, stderr)
	}
	if took := time.Since(start); took < 200*time.Millisecond || took > 5*time.Second {
		t.Fatalf("waited %s, want about the timeout", took)
	}
	for _, want := range []string{"could not update " + envFile, envfile.LockPath(envFile), "Delete"} {
		if !strings.Contains(stderr, want) {
			t.Fatalf("refusal lacks %q:\n%s", want, stderr)
		}
	}
	if got := values(t, envFile); len(got) != 1 {
		t.Fatalf("the file was written despite the lock: %v", got)
	}
}
