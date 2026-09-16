package doctor

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"go/parser"
	"go/token"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/envfile"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// recorder is the exec seam under test: every argv is kept, every answer is
// scripted, and nothing is ever executed. Its default answers describe a
// healthy docker with every image present, so a test that wants a
// failure scripts just that one.
type recorder struct {
	mu     sync.Mutex
	calls  [][]string
	answer func(args []string) (int, string, bool)
}

func (r *recorder) Run(_ time.Duration, args ...string) (int, string) {
	r.mu.Lock()
	r.calls = append(r.calls, append([]string(nil), args...))
	r.mu.Unlock()
	if r.answer != nil {
		if code, out, handled := r.answer(args); handled {
			return code, out
		}
	}
	joined := strings.Join(args, " ")
	switch {
	case strings.HasPrefix(joined, "docker version"):
		return 0, "27.1.1"
	case strings.HasPrefix(joined, "docker compose version"):
		return 0, "v2.29.1"
	case strings.HasPrefix(joined, "docker image inspect"):
		return 0, "[{}]"
	case strings.HasPrefix(joined, "docker manifest inspect"):
		return 0, "{}"
	case strings.HasPrefix(joined, "docker ps"):
		return 0, ""
	case strings.HasPrefix(joined, "git "):
		return 0, ""
	}
	return 127, "command not found: " + args[0]
}

func (r *recorder) has(prefix ...string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, call := range r.calls {
		if len(call) >= len(prefix) && strings.Join(call[:len(prefix)], " ") == strings.Join(prefix, " ") {
			return true
		}
	}
	return false
}

func (r *recorder) verbs() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []string
	for _, call := range r.calls {
		out = append(out, strings.Join(call, " "))
	}
	return out
}

func dockerFound(string) (string, error) { return "/usr/bin/docker", nil }
func noDocker(string) (string, error)    { return "", errors.New("not found") }

// closedPortURL is a backend URL nothing listens on, so the backend check
// answers WARN at once instead of touching the owner's stack on :8000.
func closedPortURL(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	url := "http://" + l.Addr().String()
	_ = l.Close()
	return url
}

// markedDir is a directory that looks like a release download: the compose
// file, .env.example, and a .env with the given content.
func markedDir(t *testing.T, env string) string {
	t.Helper()
	dir := t.TempDir()
	for name, content := range map[string]string{
		releaseCompose: "services: {}\n",
		devCompose:     "services: {}\n",
		".env.example": "# example\n",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if env != "" {
		if err := os.WriteFile(filepath.Join(dir, ".env"), []byte(env), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// anthropicShaped joins the Claude key prefix and a tail AT RUN TIME so no
// source line carries a value matching the gate's `sk-ant-` + 12 rule
// (backend/app/services/redaction/patterns.py:112). Both
// scan_repo_secrets.py (release.yml / pr-build `needs:` it) and T1's
// test_no_live_key_shaped_strings walk this file with that rule, and the
// exact-value ALLOWLIST there says to build fixtures this way rather than
// extend it (patterns.py:275-278). describeKey only ever sees the joined
// string, so the shapes under test are unchanged.
func anthropicShaped(tail string) string {
	return "sk-ant-" + tail
}

// fullEnv is a .env every check is happy with.
func fullEnv() string {
	return "ANTHROPIC_API_KEY=" + anthropicShaped("fixture-value-that-is-long-enough") + "\n" +
		"GEMINI_API_KEY=gemini-fixture-value-that-is-long-enough\n" +
		"LAZYAF_STEP_AUTH_SECRET=" + strings.Repeat("s", 64) + "\n" +
		"LAZYAF_RUNNER_AUTH_SECRET=" + strings.Repeat("r", 64) + "\n" +
		"LAZYAF_BACKEND_PORT=127.0.0.1:1\n" +
		"LAZYAF_FRONTEND_PORT=127.0.0.1:1\n"
}

func gb(n float64) func(string) (uint64, error) {
	return func(string) (uint64, error) { return uint64(n * (1 << 30)), nil }
}

// runDoctor executes Run with both streams captured and the seams filled
// from opts, and returns stdout, stderr and the exit code.
func runDoctor(t *testing.T, opts Options) (stdout, stderr string, code int) {
	t.Helper()
	if opts.Getenv == nil {
		opts.Getenv = func(string) string { return "" }
	}
	if opts.Exec == nil {
		opts.Exec = &recorder{}
	}
	if opts.LookPath == nil {
		opts.LookPath = dockerFound
	}
	if opts.FreeBytes == nil {
		opts.FreeBytes = gb(100)
	}
	if opts.Server == "" {
		opts.Server = closedPortURL(t)
	}
	out, errBuf := &bytes.Buffer{}, &bytes.Buffer{}
	savedOut, savedErr := ui.Stdout, ui.Stderr
	ui.Stdout, ui.Stderr = out, errBuf
	t.Setenv("NO_COLOR", "1")
	defer func() { ui.Stdout, ui.Stderr = savedOut, savedErr }()
	code = ui.Report(Run(opts, out))
	return out.String(), errBuf.String(), code
}

// §8.2: docker via subprocess only (preflight.py:98-136), `docker image
// inspect` / `manifest inspect` (:526-539), pull+retag remedies (:632-643).
func TestDockerChecksAreSubprocessOnly(t *testing.T) {
	t.Run("every docker question is an argv through the seam", func(t *testing.T) {
		rec := &recorder{answer: func(args []string) (int, string, bool) {
			joined := strings.Join(args, " ")
			switch {
			case joined == "docker image inspect lazyaf-claude:dev":
				return 1, "Error: No such image: lazyaf-claude:dev", true
			case strings.HasPrefix(joined, "docker image inspect ghcr.io/brennan-vanderlaan/lazyaf/backend:"):
				return 1, "Error: No such image", true
			case strings.HasPrefix(joined, "docker manifest inspect ghcr.io/brennan-vanderlaan/lazyaf/backend:"):
				return 1, "no such manifest: manifest unknown", true
			}
			return 0, "", false
		}}
		dir := markedDir(t, fullEnv())
		stdout, stderr, code := runDoctor(t, Options{Dir: dir, Exec: rec})
		if code != 1 {
			t.Fatalf("a missing release image is a FAIL; exit %d:\n%s%s", code, stdout, stderr)
		}
		for _, want := range [][]string{
			{"docker", "version", "--format", "{{.Server.Version}}"},
			{"docker", "compose", "version", "--short"},
			{"docker", "image", "inspect", "ghcr.io/brennan-vanderlaan/lazyaf/backend:latest"},
			{"docker", "manifest", "inspect", "ghcr.io/brennan-vanderlaan/lazyaf/backend:latest"},
			{"docker", "image", "inspect", "lazyaf-claude:dev"},
			{"git", "-C", dir, "check-ignore", "-q", ".env"},
		} {
			if !rec.has(want...) {
				t.Errorf("no subprocess %q; calls were:\n%s", strings.Join(want, " "), strings.Join(rec.verbs(), "\n"))
			}
		}
		for _, img := range StepImages {
			if !rec.has("docker", "image", "inspect", img) {
				t.Errorf("step image %s was not inspected", img)
			}
		}
		// The remedies name the published repository (prefix dropped) and
		// the LOCAL tag the backend looks for.
		for _, want := range []string{
			"[FAIL] 1 release image(s) do not exist at that name/tag",
			"  ghcr.io/brennan-vanderlaan/lazyaf/backend:latest",
			"[WARN] 1 of " + strconv.Itoa(len(StepImages)) + " step images missing",
			"  docker pull ghcr.io/brennan-vanderlaan/lazyaf/claude:latest",
			"  docker tag ghcr.io/brennan-vanderlaan/lazyaf/claude:latest lazyaf-claude:dev",
			"[ OK ] Docker engine 27.1.1 is running",
			"[ OK ] docker compose v2.29.1 available",
		} {
			if !strings.Contains(stdout, want) {
				t.Errorf("stdout lacks %q:\n%s", want, stdout)
			}
		}
		if strings.Contains(stdout, "lazyaf/lazyaf-claude") {
			t.Fatalf("the remote ref kept the local prefix; every pull would 404:\n%s", stdout)
		}
	})
	t.Run("a custom prefix and version flow into every remedy", func(t *testing.T) {
		rec := &recorder{answer: func(args []string) (int, string, bool) {
			if strings.HasPrefix(strings.Join(args, " "), "docker image inspect lazyaf-base:dev") {
				return 1, "", true
			}
			return 0, "", false
		}}
		dir := markedDir(t, fullEnv()+"LAZYAF_IMAGE_PREFIX=registry.local/mirror/\nLAZYAF_VERSION=v0.3.0\n")
		stdout, _, _ := runDoctor(t, Options{Dir: dir, Exec: rec})
		if !strings.Contains(stdout, "  docker pull registry.local/mirror/base:v0.3.0\n") ||
			!strings.Contains(stdout, "  docker tag registry.local/mirror/base:v0.3.0 lazyaf-base:dev\n") {
			t.Fatalf("stdout:\n%s", stdout)
		}
		if !rec.has("docker", "image", "inspect", "registry.local/mirror/backend:v0.3.0") {
			t.Fatalf("service ref not built from the .env coordinates; calls:\n%s", strings.Join(rec.verbs(), "\n"))
		}
	})
	t.Run("no docker sdk is compiled in", func(t *testing.T) {
		// The seam is the only route; go/parser over the shipped files.
		fset := token.NewFileSet()
		entries, err := os.ReadDir(".")
		if err != nil {
			t.Fatal(err)
		}
		for _, e := range entries {
			if !strings.HasSuffix(e.Name(), ".go") || strings.HasSuffix(e.Name(), "_test.go") {
				continue
			}
			f, err := parser.ParseFile(fset, e.Name(), nil, parser.ImportsOnly)
			if err != nil {
				t.Fatal(err)
			}
			for _, imp := range f.Imports {
				if strings.Contains(strings.ToLower(imp.Path.Value), "docker") {
					t.Errorf("%s imports %s; docker is a subprocess here, never an SDK", e.Name(), imp.Path.Value)
				}
			}
		}
	})
	t.Run("offline skips every registry lookup", func(t *testing.T) {
		rec := &recorder{answer: func(args []string) (int, string, bool) {
			if strings.HasPrefix(strings.Join(args, " "), "docker image inspect ghcr.io") {
				return 1, "", true
			}
			return 0, "", false
		}}
		stdout, _, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), Exec: rec, Offline: true})
		if rec.has("docker", "manifest", "inspect") {
			t.Fatal("--offline still asked the registry")
		}
		if !strings.Contains(stdout, "registry check skipped (--offline)") || !strings.Contains(stdout, "Backend check skipped (--offline)") {
			t.Fatalf("stdout:\n%s", stdout)
		}
		if code != 0 {
			t.Fatalf("an unverifiable image is a WARN, not a FAIL; exit %d:\n%s", code, stdout)
		}
	})
}

// §8.2: disk WARN 15 GB / FAIL 5 GB (preflight.py:58-59).
func TestDiskThresholds(t *testing.T) {
	for _, tc := range []struct {
		free float64
		want status
	}{
		{0, statusFail}, {4.9, statusFail}, {5, statusWarn}, {14.9, statusWarn}, {15, statusOK}, {500, statusOK},
	} {
		if got, _, _ := diskVerdict(tc.free); got != tc.want {
			t.Errorf("diskVerdict(%.1f) = %s, want %s", tc.free, got, tc.want)
		}
	}
	t.Run("the verdict reaches the report with the figure", func(t *testing.T) {
		out := &bytes.Buffer{}
		checkDisk(&reporter{w: out}, ".", gb(4.5))
		if !strings.Contains(out.String(), "[FAIL] Only 4.5 GB free") {
			t.Fatalf("report:\n%s", out.String())
		}
		out.Reset()
		checkDisk(&reporter{w: out}, ".", func(string) (uint64, error) { return 0, errors.New("statfs: nope") })
		if !strings.Contains(out.String(), "[WARN] Could not read free disk space") || !strings.Contains(out.String(), "statfs: nope") {
			t.Fatalf("an unreadable figure is a WARN with the reason:\n%s", out.String())
		}
	})
	t.Run("the platform call answers for a real directory", func(t *testing.T) {
		n, err := freeBytes(t.TempDir())
		if err != nil || n == 0 {
			t.Fatalf("freeBytes = %d, %v", n, err)
		}
	})
}

// §8.2: env verdicts (preflight.py:234-243, :251-262, :213-226, :327-391).
func TestEnvVerdicts(t *testing.T) {
	envOnly := func(t *testing.T, dir string, rec *recorder, getenv func(string) string) string {
		t.Helper()
		if rec == nil {
			rec = &recorder{}
		}
		if getenv == nil {
			getenv = func(string) string { return "" }
		}
		out := &bytes.Buffer{}
		checkEnv(&reporter{w: out}, dir, Options{Exec: rec, Getenv: getenv})
		return out.String()
	}
	t.Run(".env missing names lazyaf init", func(t *testing.T) {
		out := envOnly(t, markedDir(t, ""), nil, nil)
		if !strings.Contains(out, "[FAIL] .env not found") || !strings.Contains(out, "  lazyaf init") {
			t.Fatalf("report:\n%s", out)
		}
		if strings.Contains(out, "bootstrap_secrets") {
			t.Fatalf("the retired script is named:\n%s", out)
		}
	})
	t.Run("git check-ignore verdicts", func(t *testing.T) {
		for code, want := range map[int]string{0: "[ OK ] .env is gitignored", 1: "[FAIL] .env is NOT ignored by git", 128: ""} {
			rec := &recorder{answer: func(args []string) (int, string, bool) {
				if args[0] == "git" {
					return code, "", true
				}
				return 0, "", false
			}}
			out := envOnly(t, markedDir(t, fullEnv()), rec, nil)
			if want == "" {
				if strings.Contains(out, "gitignored") || strings.Contains(out, "ignored by git") {
					t.Fatalf("git exit %d is not a checkout; no line expected:\n%s", code, out)
				}
				continue
			}
			if !strings.Contains(out, want) {
				t.Fatalf("git exit %d: report lacks %q:\n%s", code, want, out)
			}
		}
	})
	t.Run("api keys are judged by shape only", func(t *testing.T) {
		// Map keys are joined at run time (see anthropicShaped); the
		// shapes are: an x-run placeholder behind the right prefix, a
		// short value behind it, and a real-looking one.
		for value, want := range map[string]status{
			"":                                       statusWarn,
			"changeme":                               statusFail,
			anthropicShaped(strings.Repeat("x", 25)): statusFail,
			"not-the-prefix-but-long-enough-key":     statusWarn,
			"sk-ant-short":                           statusWarn,
			anthropicShaped("a-real-looking-key-0123456"): statusOK,
		} {
			got, msg := describeKey("ANTHROPIC_API_KEY", value, "sk-ant-", "Claude")
			if got != want {
				t.Errorf("describeKey(%q) = %s (%s), want %s", value, got, msg, want)
			}
			if value != "" && strings.Contains(msg, value) {
				t.Errorf("the value %q reached the message: %s", value, msg)
			}
		}
	})
	t.Run("shared secret verdicts", func(t *testing.T) {
		cases := []struct {
			name string
			env  string
			get  func(string) string
			want []string
			code status
		}{
			{"generated values are ok", fullEnv(), nil,
				[]string{"[ OK ] LAZYAF_STEP_AUTH_SECRET is set (", "[ OK ] LAZYAF_RUNNER_AUTH_SECRET is set ("}, statusOK},
			{"_FILE in the file satisfies", "LAZYAF_STEP_AUTH_SECRET_FILE=/run/secrets/step\nLAZYAF_RUNNER_AUTH_SECRET_FILE=/run/secrets/runner\n", nil,
				[]string{"[ OK ] LAZYAF_STEP_AUTH_SECRET supplied by LAZYAF_STEP_AUTH_SECRET_FILE (/run/secrets/step)"}, statusOK},
			{"_FILE in the environment satisfies", "", func(k string) string {
				if strings.HasSuffix(k, "_FILE") {
					return "/run/secrets/x"
				}
				return ""
			}, []string{"supplied by LAZYAF_STEP_AUTH_SECRET_FILE", "supplied by LAZYAF_RUNNER_AUTH_SECRET_FILE"}, statusOK},
			{"retired default is a FAIL", "LAZYAF_STEP_AUTH_SECRET=" + envfile.RetiredPublicSecrets[1] + "\nLAZYAF_RUNNER_AUTH_SECRET=" + strings.Repeat("r", 40) + "\n", nil,
				[]string{"[FAIL] Retired PUBLIC default still in .env for: LAZYAF_STEP_AUTH_SECRET", "  lazyaf init"}, statusFail},
			{"placeholder is a FAIL", "LAZYAF_STEP_AUTH_SECRET=changeme\nLAZYAF_RUNNER_AUTH_SECRET=\n", nil,
				[]string{"[FAIL] Not set: LAZYAF_STEP_AUTH_SECRET, LAZYAF_RUNNER_AUTH_SECRET", "  lazyaf init", "_FILE form"}, statusFail},
			{"unset is a FAIL", "", nil,
				[]string{"[FAIL] Not set: LAZYAF_STEP_AUTH_SECRET, LAZYAF_RUNNER_AUTH_SECRET"}, statusFail},
			{"short is a WARN", "LAZYAF_STEP_AUTH_SECRET=only-thirty-one-characters-xxx1\nLAZYAF_RUNNER_AUTH_SECRET=" + strings.Repeat("r", 32) + "\n", nil,
				[]string{"[WARN] Under 32 characters: LAZYAF_STEP_AUTH_SECRET", "[ OK ] LAZYAF_RUNNER_AUTH_SECRET is set"}, statusWarn},
		}
		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				get := tc.get
				if get == nil {
					get = func(string) string { return "" }
				}
				out := &bytes.Buffer{}
				r := &reporter{w: out}
				checkSharedSecrets(r, envfile.ReadValues(envfile.SplitLines(tc.env)), get)
				for _, want := range tc.want {
					if !strings.Contains(out.String(), want) {
						t.Errorf("report lacks %q:\n%s", want, out.String())
					}
				}
				if r.count(tc.code) == 0 {
					t.Errorf("no %s verdict:\n%s", tc.code, out.String())
				}
				if strings.Contains(out.String(), "bootstrap_secrets") {
					t.Errorf("the retired script is named:\n%s", out.String())
				}
			})
		}
	})
}

// §8.2 / preflight.py:13-15: never a value in the output. Every key gets a
// sentinel that would be easy to echo by accident; the transcript of a
// FULL run must hold none of them, under every verdict a value can get.
func TestNoValueIsEverPrinted(t *testing.T) {
	sentinels := map[string]string{
		"ANTHROPIC_API_KEY":         "SENTINEL-ANTHROPIC-KEY-0123456789abcdef",
		"GEMINI_API_KEY":            "SENTINEL-GEMINI-KEY-0123456789abcdef",
		"LAZYAF_STEP_AUTH_SECRET":   "SENTINEL-STEP-SECRET-0123456789abcdefghijklmnop",
		"LAZYAF_RUNNER_AUTH_SECRET": "SENTINEL-SHORT",
	}
	env := ""
	for k, v := range sentinels {
		env += k + "=" + v + "\n"
	}
	env += "LAZYAF_BACKEND_PORT=127.0.0.1:1\nLAZYAF_FRONTEND_PORT=1\n"
	for _, offline := range []bool{false, true} {
		for _, dev := range []bool{false, true} {
			stdout, stderr, _ := runDoctor(t, Options{Dir: markedDir(t, env), Offline: offline, Dev: dev})
			transcript := stdout + stderr
			for key, value := range sentinels {
				if strings.Contains(transcript, value) {
					t.Fatalf("the value of %s reached the output (offline=%v dev=%v):\n%s", key, offline, dev, transcript)
				}
			}
		}
	}
	// And the ambient environment is read for the shared secrets but never
	// echoed either.
	getenv := func(k string) string {
		if k == "LAZYAF_STEP_AUTH_SECRET" {
			return "SENTINEL-FROM-THE-SHELL-0123456789abcdefghijklmnop"
		}
		return ""
	}
	stdout, stderr, _ := runDoctor(t, Options{Dir: markedDir(t, "LAZYAF_RUNNER_AUTH_SECRET=changeme\n"), Getenv: getenv})
	if strings.Contains(stdout+stderr, "SENTINEL-FROM-THE-SHELL") {
		t.Fatalf("an exported value reached the output:\n%s%s", stdout, stderr)
	}
	if !strings.Contains(stdout, "[ OK ] LAZYAF_STEP_AUTH_SECRET is set") {
		t.Fatalf("the exported value was not honoured:\n%s", stdout)
	}
}

// §8.2 / preflight.py:8-11: pulls nothing, writes nothing, starts nothing.
func TestNoSideEffects(t *testing.T) {
	dir := markedDir(t, fullEnv())
	before := snapshot(t, dir)
	rec := &recorder{answer: func(args []string) (int, string, bool) {
		// Everything is missing, so every remedy path is exercised.
		if strings.HasPrefix(strings.Join(args, " "), "docker image inspect") {
			return 1, "No such image", true
		}
		if strings.HasPrefix(strings.Join(args, " "), "docker manifest inspect") {
			return 1, "manifest unknown", true
		}
		return 0, "", false
	}}
	runDoctor(t, Options{Dir: dir, Exec: rec})
	runDoctor(t, Options{Dir: dir, Exec: rec, Dev: true})
	runDoctor(t, Options{Dir: dir, Exec: rec, Offline: true})

	allowed := []string{
		"docker version", "docker compose version", "docker image inspect", "docker manifest inspect",
		"docker ps", "git -C",
	}
	for _, verb := range rec.verbs() {
		ok := false
		for _, a := range allowed {
			if strings.HasPrefix(verb, a) {
				ok = true
			}
		}
		if !ok {
			t.Errorf("doctor ran %q, which is not a read-only question", verb)
		}
		for _, forbidden := range []string{" pull ", " run ", " up", " build", " tag ", " push ", " create ", " start ", " rm ", " compose up"} {
			if strings.Contains(verb+" ", forbidden) {
				t.Errorf("doctor ran %q: it must never change the machine", verb)
			}
		}
	}
	if len(rec.verbs()) == 0 {
		t.Fatal("no subprocess ran at all; the property guards nothing")
	}
	if after := snapshot(t, dir); after != before {
		t.Fatalf("doctor changed the directory:\nbefore %s\nafter  %s", before, after)
	}
}

// snapshot is every entry's name, size and content hash under dir.
func snapshot(t *testing.T, dir string) string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var parts []string
	for _, e := range entries {
		raw, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(raw)
		parts = append(parts, e.Name()+":"+strconv.Itoa(len(raw))+":"+string(sum[:4]))
	}
	return strings.Join(parts, ",")
}

// §8.2 / preflight.py:731-735: never a traceback. A panic inside a check
// becomes the stated refusal, exit 1, with the stack only on request.
func TestNeverATraceback(t *testing.T) {
	boom := func(string) (uint64, error) { panic("disk exploded") }
	t.Run("the refusal names the bug and where to report it", func(t *testing.T) {
		stdout, stderr, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), FreeBytes: boom})
		if code != 1 {
			t.Fatalf("exit %d, want 1:\n%s%s", code, stdout, stderr)
		}
		for _, want := range []string{
			"doctor hit an unexpected problem and stopped: disk exploded",
			"This is a bug in lazyaf doctor, not a problem with your setup.",
			IssuesURL,
			"--debug-trace",
		} {
			if !strings.Contains(stderr, want) {
				t.Fatalf("stderr lacks %q:\n%s", want, stderr)
			}
		}
		if strings.Contains(stderr, "goroutine ") {
			t.Fatalf("a stack was printed without --debug-trace:\n%s", stderr)
		}
	})
	t.Run("--debug-trace includes the stack", func(t *testing.T) {
		_, stderr, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), FreeBytes: boom, DebugTrace: true})
		if code != 1 || !strings.Contains(stderr, "goroutine ") || !strings.Contains(stderr, "disk.go") {
			t.Fatalf("exit %d, stderr:\n%s", code, stderr)
		}
	})
}

// §8.2: the docker-down short-circuit (preflight.py:693-704) and the exit
// rule (:705-721): FAIL exits 1, WARN never does.
func TestDockerDownShortCircuit(t *testing.T) {
	t.Run("no docker cli stops after the env check", func(t *testing.T) {
		rec := &recorder{}
		stdout, _, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), Exec: rec, LookPath: noDocker})
		if code != 1 || !strings.Contains(stdout, "[FAIL] Docker CLI not found") {
			t.Fatalf("exit %d:\n%s", code, stdout)
		}
		if !strings.Contains(stdout, "[ OK ] .env found") {
			t.Fatalf("the env check must still run:\n%s", stdout)
		}
		if rec.has("docker") || strings.Contains(stdout, "Port ") || strings.Contains(stdout, "step images") {
			t.Fatalf("checks that need docker ran anyway:\n%s", stdout)
		}
	})
	t.Run("a daemon that does not answer quotes docker", func(t *testing.T) {
		rec := &recorder{answer: func(args []string) (int, string, bool) {
			if strings.HasPrefix(strings.Join(args, " "), "docker version") {
				return 1, "error during connect: this error may indicate that the docker daemon is not running", true
			}
			return 0, "", false
		}}
		stdout, _, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), Exec: rec})
		if code != 1 || !strings.Contains(stdout, "[FAIL] Docker daemon is not responding") || !strings.Contains(stdout, "  error during connect") {
			t.Fatalf("exit %d:\n%s", code, stdout)
		}
	})
	t.Run("warnings alone are READY exit 0", func(t *testing.T) {
		stdout, _, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()+"GEMINI_API_KEY=\n")})
		if code != 0 {
			t.Fatalf("exit %d:\n%s", code, stdout)
		}
		if !strings.Contains(stdout, "READY, with ") || !strings.Contains(stdout, "docker compose -f "+releaseCompose+" pull") {
			t.Fatalf("stdout:\n%s", stdout)
		}
	})
	t.Run("a missing compose file names --dir and the checkout", func(t *testing.T) {
		stdout, stderr, code := runDoctor(t, Options{Dir: t.TempDir()})
		if code != 1 || !strings.Contains(stdout, "[FAIL] "+releaseCompose+" not found in ") || !strings.Contains(stdout, "--dir") {
			t.Fatalf("exit %d:\n%s%s", code, stdout, stderr)
		}
		if !strings.Contains(stderr, "NOT READY:") {
			t.Fatalf("stderr:\n%s", stderr)
		}
	})
}

// §8.2: check_backend with the describe_server provenance line.
func TestBackendCheck(t *testing.T) {
	t.Run("an answering backend is OK with its provenance", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/health" {
				t.Errorf("asked %s, want /health", r.URL.Path)
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"status":"ok"}`))
		}))
		defer srv.Close()
		stdout, _, _ := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), Server: srv.URL})
		if !strings.Contains(stdout, "[ OK ] Backend at "+srv.URL+" answered GET /health") {
			t.Fatalf("stdout:\n%s", stdout)
		}
		if !strings.Contains(stdout, "LazyAF backend: "+srv.URL+" (from --server)") {
			t.Fatalf("the provenance line is missing:\n%s", stdout)
		}
	})
	t.Run("a silent backend is a WARN that names the url and how to change it", func(t *testing.T) {
		url := closedPortURL(t)
		stdout, _, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), Server: url})
		if code != 0 {
			t.Fatalf("doctor runs before `up`; a silent backend must not fail it. exit %d:\n%s", code, stdout)
		}
		for _, want := range []string{"[WARN] No LazyAF backend answering at " + url, "(from --server)", "--server <url>", "LAZYAF_SERVER=<url>", "The client said:"} {
			if !strings.Contains(stdout, want) {
				t.Fatalf("stdout lacks %q:\n%s", want, stdout)
			}
		}
	})
	t.Run("a backend that is not the api is said so", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte("<!doctype html>"))
		}))
		defer srv.Close()
		stdout, _, _ := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), Server: srv.URL})
		if !strings.Contains(stdout, "[WARN] No LazyAF backend answering at") || !strings.Contains(stdout, "not the LazyAF API") {
			t.Fatalf("stdout:\n%s", stdout)
		}
	})
	t.Run("a schemeless url is a FAIL with the fix", func(t *testing.T) {
		stdout, _, code := runDoctor(t, Options{Dir: markedDir(t, fullEnv()), Server: "localhost:8790"})
		if code != 1 || !strings.Contains(stdout, "[FAIL] Backend URL cannot be used") || !strings.Contains(stdout, "http://localhost:8790") {
			t.Fatalf("exit %d:\n%s", code, stdout)
		}
	})
}

// The ports check end to end through the reporter: a held port is a FAIL
// unless docker owns it, in which case it is a WARN (preflight.py:486-505).
func TestPortsCheckVerdicts(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	port := strconv.Itoa(l.Addr().(*net.TCPAddr).Port)
	values := map[string]string{"LAZYAF_BACKEND_PORT": "127.0.0.1:" + port, "LAZYAF_FRONTEND_PORT": port}

	out := &bytes.Buffer{}
	checkPorts(&reporter{w: out}, values, Options{Exec: &recorder{}})
	if !strings.Contains(out.String(), "[FAIL] Port "+port+" is in use (needed for the backend API)") {
		t.Fatalf("report:\n%s", out.String())
	}

	out.Reset()
	owned := &recorder{answer: func(args []string) (int, string, bool) {
		if strings.HasPrefix(strings.Join(args, " "), "docker ps") {
			return 0, "lazyaf-backend-1 (lazyaf/backend:main)", true
		}
		return 0, "", false
	}}
	checkPorts(&reporter{w: out}, values, Options{Exec: owned})
	if !strings.Contains(out.String(), "[WARN] Port "+port+" is already used by a container: lazyaf-backend-1 (lazyaf/backend:main)") {
		t.Fatalf("report:\n%s", out.String())
	}
	if !owned.has("docker", "ps", "--filter", "publish="+port) {
		t.Fatalf("the owner hint did not ask docker; calls:\n%s", strings.Join(owned.verbs(), "\n"))
	}
}
