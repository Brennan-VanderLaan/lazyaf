package reconcile_test

// Port of tdd/unit/scripts/test_cli_tests_reconcile.py. An EXTERNAL test
// package on purpose: the refusals under test are the command's behaviour
// (`lazyaf tests reconcile ...`), so these drive the real cobra tree through
// cmd.NewRootWith + cmd.Run exactly as main.go does, with --server pointed
// at an httptest.Server; the manifest helpers and the collector are called
// directly. reconcile_test may import cmd (which imports reconcile) without
// a cycle - that is what external test packages are for.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/cmd"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/gitx"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/reconcile"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/terminal"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

type outcome struct {
	code           int
	stdout, stderr string
}

func (o outcome) all() string { return o.stdout + o.stderr }

// run is `lazyaf tests reconcile args...` against the given server URL.
func run(t *testing.T, server string, args ...string) outcome {
	t.Helper()
	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	savedOut, savedErr := ui.Stdout, ui.Stderr
	ui.Stdout, ui.Stderr = stdout, stderr
	t.Setenv("NO_COLOR", "1")
	defer func() { ui.Stdout, ui.Stderr = savedOut, savedErr }()
	deps := &cmd.Deps{
		Git: &gitx.Recorder{},
		Dial: func(ctx context.Context, url, token string) (terminal.Socket, error) {
			t.Fatalf("reconcile dialed a websocket")
			return nil, nil
		},
	}
	root := cmd.NewRootWith(deps)
	root.SetArgs(append([]string{"--server", server, "tests", "reconcile"}, args...))
	code := cmd.Run(context.Background(), root)
	return outcome{code: code, stdout: stdout.String(), stderr: stderr.String()}
}

// noHTTP is the `no_http` fixture: fail loudly if a refusal path ever
// reaches the network.
func noHTTP(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("reconcile must refuse BEFORE calling the API; got %s %s", r.Method, r.URL.Path)
		http.Error(w, "unexpected", http.StatusTeapot)
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

// posted is the one POST the command makes: the raw wire bytes (the
// assertions compare THOSE - a re-marshalled map would reorder keys) and
// the decoded envelope.
type posted struct {
	raw  string
	body map[string]any
}

// refs is the "refs" array exactly as it went on the wire.
func (p *posted) refs(t *testing.T) string {
	t.Helper()
	i := strings.Index(p.raw, `"refs":`)
	if i < 0 {
		t.Fatalf("no refs in the posted body: %s", p.raw)
	}
	return strings.TrimSuffix(p.raw[i+len(`"refs":`):], "}")
}

// reconcileAPI records the one POST the command makes.
func reconcileAPI(t *testing.T) (url string, got *posted) {
	t.Helper()
	got = &posted{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/test-refs/reconcile" {
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
			http.NotFound(w, r)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		got.raw = string(raw)
		if err := json.Unmarshal(raw, &got.body); err != nil {
			t.Errorf("body is not JSON: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"created":1,"updated":0,"orphaned":0}`))
	}))
	t.Cleanup(srv.Close)
	return srv.URL, got
}

func writeJSON(t *testing.T, path string, v any) string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func mustContain(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, w := range wants {
		if !strings.Contains(text, w) {
			t.Fatalf("output lacks %q:\n%s", w, text)
		}
	}
}

func resultsManifest(t *testing.T, dir string) string {
	t.Helper()
	return writeJSON(t, filepath.Join(dir, "test_results.json"), map[string]any{
		"version": 1,
		"results": []any{map[string]any{"lazyaf_test_id": "us1.ran", "status": "passed", "duration_ms": 3, "file_path": "tdd/unit/test_x.py"}},
	})
}

// -----------------------------------------------------------------------------
// The core refusal: no source, and no default
// -----------------------------------------------------------------------------

func TestRefusesAmbiguousInput(t *testing.T) {
	t.Run("no_source_refuses", func(t *testing.T) {
		o := run(t, noHTTP(t), "repo-123")
		if o.code != 1 {
			t.Fatalf("exit %d, want 1:\n%s", o.code, o.all())
		}
		mustContain(t, o.all(), "Refusing to reconcile")
	})
	t.Run("no_source_message_names_the_orphaning_hazard", func(t *testing.T) {
		o := run(t, noHTTP(t), "repo-123")
		mustContain(t, strings.ToLower(o.all()), "orphan")
		// Names BOTH escape hatches so the user can act on the message.
		mustContain(t, o.all(), "--from-collect", "--refs")
	})
	t.Run("no_source_does_not_fall_back_to_env_var", func(t *testing.T) {
		// The old default. LAZYAF_TEST_RESULTS_PATH is a per-STEP path the
		// control runtime injects; honoring it here reconciles one step's
		// run against the whole repo.
		manifest := writeJSON(t, filepath.Join(t.TempDir(), "test_results.json"),
			map[string]any{"version": 1, "results": []any{map[string]any{"lazyaf_test_id": "a"}}})
		t.Setenv("LAZYAF_TEST_RESULTS_PATH", manifest)
		o := run(t, noHTTP(t), "repo-123")
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "Refusing to reconcile")
	})
	t.Run("no_source_does_not_fall_back_to_cwd_manifest", func(t *testing.T) {
		// The other old default: ./test_results.json.
		dir := t.TempDir()
		writeJSON(t, filepath.Join(dir, "test_results.json"),
			map[string]any{"version": 1, "results": []any{map[string]any{"lazyaf_test_id": "a"}}})
		t.Chdir(dir)
		o := run(t, noHTTP(t), "repo-123")
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "Refusing to reconcile")
	})
	t.Run("both_sources_refuses", func(t *testing.T) {
		refs := writeJSON(t, filepath.Join(t.TempDir(), "refs.json"), map[string]any{"refs": []any{}})
		o := run(t, noHTTP(t), "repo-123", "--refs", refs, "--from-collect")
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "mutually exclusive")
	})
	t.Run("a_missing_repo_id_is_a_usage_error", func(t *testing.T) {
		o := run(t, noHTTP(t))
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.all(), "REPO_ID", "lazyaf tests reconcile abc123 --from-collect")
	})
}

// -----------------------------------------------------------------------------
// A results manifest is not a declared set
// -----------------------------------------------------------------------------

func TestRefusesResultsManifest(t *testing.T) {
	t.Run("results_manifest_refused_by_default", func(t *testing.T) {
		o := run(t, noHTTP(t), "repo-123", "--refs", resultsManifest(t, t.TempDir()))
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "RESULTS manifest")
	})
	t.Run("refusal_explains_the_partial_run_problem", func(t *testing.T) {
		o := run(t, noHTTP(t), "repo-123", "--refs", resultsManifest(t, t.TempDir()))
		mustContain(t, o.all(), "only the tests that RAN", "--allow-results-manifest")
	})
	t.Run("manifest_alias_is_refused_the_same_way", func(t *testing.T) {
		// --manifest is kept as an alias of --refs; it must not be a way
		// back into the old behavior.
		o := run(t, noHTTP(t), "repo-123", "--manifest", resultsManifest(t, t.TempDir()))
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "RESULTS manifest")
		o = run(t, noHTTP(t), "repo-123", "-m", resultsManifest(t, t.TempDir()))
		mustContain(t, o.all(), "RESULTS manifest")
	})
	t.Run("explicit_opt_in_is_accepted", func(t *testing.T) {
		// --allow-results-manifest proceeds, but warns.
		url, posted := reconcileAPI(t)
		o := run(t, url, "repo-123", "--refs", resultsManifest(t, t.TempDir()), "--allow-results-manifest")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, "Warning")
		if posted.body["repo_id"] != "repo-123" {
			t.Fatalf("posted = %s", posted.raw)
		}
		if refs := posted.refs(t); refs != `[{"lazyaf_test_id":"us1.ran","file_path":"tdd/unit/test_x.py"}]` {
			t.Fatalf("posted refs = %s", refs)
		}
		mustContain(t, o.stdout, "Reconciled!", "created: 1")
	})
}

// -----------------------------------------------------------------------------
// An empty declared set would orphan everything
// -----------------------------------------------------------------------------

func TestRefusesEmptyDeclaredSet(t *testing.T) {
	t.Run("empty_refs_manifest_refused", func(t *testing.T) {
		refs := writeJSON(t, filepath.Join(t.TempDir(), "refs.json"), map[string]any{"refs": []any{}})
		o := run(t, noHTTP(t), "repo-123", "--refs", refs)
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "EMPTY")
		mustContain(t, strings.ToLower(o.all()), "orphan")
	})
	t.Run("missing_manifest_refused", func(t *testing.T) {
		o := run(t, noHTTP(t), "repo-123", "--refs", filepath.Join(t.TempDir(), "nope.json"))
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, strings.ToLower(o.all()), "not found")
	})
	t.Run("malformed_manifest_refused", func(t *testing.T) {
		refs := filepath.Join(t.TempDir(), "refs.json")
		if err := os.WriteFile(refs, []byte(`{"totally": "wrong"}`), 0o644); err != nil {
			t.Fatal(err)
		}
		o := run(t, noHTTP(t), "repo-123", "--refs", refs)
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "refs")
		bad := filepath.Join(t.TempDir(), "bad.json")
		if err := os.WriteFile(bad, []byte(`{not json`), 0o644); err != nil {
			t.Fatal(err)
		}
		o = run(t, noHTTP(t), "repo-123", "--refs", bad)
		if o.code != 1 {
			t.Fatalf("exit %d, want 1", o.code)
		}
		mustContain(t, o.all(), "not valid JSON")
	})
}

// -----------------------------------------------------------------------------
// Manifest classification / normalization
// -----------------------------------------------------------------------------

func TestManifestHelpers(t *testing.T) {
	t.Run("classify", func(t *testing.T) {
		cases := map[string]any{
			"results": map[string]any{"version": 1, "results": []any{}},
			"refs":    map[string]any{"refs": []any{}},
			"list":    []any{},
		}
		for want, data := range cases {
			if got := reconcile.Classify(data); got != want {
				t.Errorf("Classify(%v) = %q, want %q", data, got, want)
			}
		}
		if got := reconcile.Classify(map[string]any{"nope": 1}); got != "unknown" {
			t.Errorf("Classify({nope}) = %q", got)
		}
		if got := reconcile.Classify("string"); got != "unknown" {
			t.Errorf("Classify(string) = %q", got)
		}
	})
	t.Run("normalize_dedupes_and_keeps_first_path", func(t *testing.T) {
		refs := reconcile.Normalize([]any{
			map[string]any{"lazyaf_test_id": "a", "file_path": "x.py"},
			map[string]any{"lazyaf_test_id": "a", "file_path": "y.py"},
			map[string]any{"lazyaf_test_id": "", "file_path": "z.py"},
			map[string]any{"no_id": true},
			"not-a-dict",
			map[string]any{"lazyaf_test_id": "b"},
		})
		got, _ := json.Marshal(refs)
		want := `[{"lazyaf_test_id":"a","file_path":"x.py"},{"lazyaf_test_id":"b","file_path":null}]`
		if string(got) != want {
			t.Fatalf("Normalize = %s\nwant      %s", got, want)
		}
	})
	t.Run("bare_list_manifest_accepted", func(t *testing.T) {
		refs := writeJSON(t, filepath.Join(t.TempDir(), "refs.json"),
			[]any{map[string]any{"lazyaf_test_id": "a", "file_path": "t.py"}})
		url, posted := reconcileAPI(t)
		o := run(t, url, "repo-123", "--refs", refs)
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		if sent := posted.refs(t); sent != `[{"lazyaf_test_id":"a","file_path":"t.py"}]` {
			t.Fatalf("posted refs = %s", sent)
		}
	})
}

// -----------------------------------------------------------------------------
// --from-collect: the full-collection mode, against REAL pytest
// -----------------------------------------------------------------------------

// suite copies one testdata fixture into a fresh directory as
// tests/<name>.py (see testdata/README.md for why the fixtures are not
// named test_*.py in place).
func suite(t *testing.T, fixture, name string) string {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("testdata", fixture))
	if err != nil {
		t.Fatalf("fixture %s: %v", fixture, err)
	}
	dir := filepath.Join(t.TempDir(), "suite")
	if err := os.MkdirAll(filepath.Join(dir, "tests"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "tests", name), src, 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

// requirePytest names the interpreter the collector will run, or fails
// naming LAZYAF_PYTHON. By design (§10.3) this is red on a box without
// pytest; the tier image has it.
func requirePytest(t *testing.T) {
	t.Helper()
	py, err := reconcile.ResolvePython("", os.Getenv, exec.LookPath, reconcile.ProbePython)
	if err != nil {
		t.Fatalf("no Python for --from-collect: %v\nSet %s to an interpreter with pytest installed (backend/.venv's python).", err, reconcile.PythonEnvVar)
	}
	check := exec.Command(py.Argv[0], append(py.Argv[1:], "-c", "import pytest")...)
	if out, err := check.CombinedOutput(); err != nil {
		t.Fatalf("%s (from %s) cannot import pytest: %v\n%s\nSet %s to an interpreter with pytest installed.", py.Argv[0], py.Source, err, out, reconcile.PythonEnvVar)
	}
}

func TestFromCollect(t *testing.T) {
	requirePytest(t)
	t.Run("collect_finds_every_marked_test", func(t *testing.T) {
		// The declared set, not the executed set: an unmarked test is
		// excluded and BOTH marked tests appear even though nothing ran.
		dir := suite(t, "declared_suite.py", "test_declared.py")
		var notes []string
		refs, err := reconcile.Collect(context.Background(), reconcile.Options{CollectPath: dir},
			func(s string) { notes = append(notes, s) })
		if err != nil {
			t.Fatalf("Collect: %v", err)
		}
		var ids, paths []string
		for _, r := range refs {
			ids = append(ids, r.LazyafTestID)
			if r.FilePath != nil {
				paths = append(paths, *r.FilePath)
			}
		}
		if strings.Join(ids, ",") != "demo.alpha,demo.beta" {
			t.Fatalf("ids = %v", ids)
		}
		if strings.Join(paths, ",") != "tests/test_declared.py,tests/test_declared.py" {
			t.Fatalf("paths = %v (must be suite-root-relative with / separators)", paths)
		}
		// The "Collecting:" line names the interpreter and where it came from (§5).
		if len(notes) != 1 || !strings.HasPrefix(notes[0], "Collecting: ") || !strings.Contains(notes[0], "interpreter from ") {
			t.Fatalf("notes = %v", notes)
		}
	})
	t.Run("collection_error_refuses", func(t *testing.T) {
		// A partial collection is the exact ambiguity this mode avoids.
		dir := suite(t, "broken_suite.py", "test_broken.py")
		_, err := reconcile.Collect(context.Background(), reconcile.Options{CollectPath: dir}, nil)
		if err == nil {
			t.Fatal("a broken suite collected without a refusal")
		}
		f, ok := err.(*ui.Failure)
		if !ok || f.Code != 1 {
			t.Fatalf("refusal is %T %v, want *ui.Failure exit 1", err, err)
		}
		mustContain(t, f.Summary, "Refusing to reconcile", "collection errors")
		mustContain(t, f.Detail, "nonexistent_module_xyz")
	})
	t.Run("the_command_end_to_end_posts_the_collected_set", func(t *testing.T) {
		dir := suite(t, "declared_suite.py", "test_declared.py")
		url, posted := reconcileAPI(t)
		o := run(t, url, "repo-123", "--from-collect", "-C", dir)
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "Collecting: ", "Reconciling 2 test ref(s) for repo repo-123 from pytest --collect-only in", "Reconciled!")
		if sent := posted.refs(t); sent != `[{"lazyaf_test_id":"demo.alpha","file_path":"tests/test_declared.py"},{"lazyaf_test_id":"demo.beta","file_path":"tests/test_declared.py"}]` {
			t.Fatalf("posted refs = %s", sent)
		}
	})
	t.Run("a_bogus_python_is_a_refusal_naming_the_flag", func(t *testing.T) {
		dir := suite(t, "declared_suite.py", "test_declared.py")
		o := run(t, noHTTP(t), "repo-123", "--from-collect", "-C", dir, "--python", filepath.Join(dir, "no-such-python"))
		if o.code != 1 {
			t.Fatalf("exit %d, want 1:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, "no-such-python", "--python")
	})
}

// The interpreter resolution order §5 fixes, with every seam injected.
func TestResolvePythonOrder(t *testing.T) {
	env := map[string]string{}
	getenv := func(k string) string { return env[k] }
	found := map[string]string{}
	lookPath := func(name string) (string, error) {
		if p, ok := found[name]; ok {
			return p, nil
		}
		return "", exec.ErrNotFound
	}
	// The prober stands in for `<candidate> -c "import sys"`: the Microsoft
	// Store alias is the one candidate that resolves but does not run.
	const storeStub = `C:\Users\x\AppData\Local\Microsoft\WindowsApps\python3.exe`
	var probed []string
	probe := func(argv []string) error {
		probed = append(probed, argv[0])
		if argv[0] == storeStub {
			return errors.New("exit status 9009: Python was not found; run without arguments to install from the Microsoft Store")
		}
		return nil
	}
	if got, err := reconcile.ResolvePython("/x/py", getenv, lookPath, probe); err != nil || got.Argv[0] != "/x/py" || got.Source != "--python" {
		t.Fatalf("explicit: %+v %v", got, err)
	}
	env[reconcile.PythonEnvVar] = "/env/py"
	if got, _ := reconcile.ResolvePython("", getenv, lookPath, probe); got.Argv[0] != "/env/py" || got.Source != "$"+reconcile.PythonEnvVar {
		t.Fatalf("env: %+v", got)
	}
	if len(probed) != 0 {
		t.Fatalf("an explicit interpreter is taken as given, not probed: %v", probed)
	}
	delete(env, reconcile.PythonEnvVar)
	found["python"] = "/usr/bin/python"
	if got, _ := reconcile.ResolvePython("", getenv, lookPath, probe); got.Argv[0] != "/usr/bin/python" {
		t.Fatalf("python: %+v", got)
	}
	found["python3"] = "/usr/bin/python3"
	if got, _ := reconcile.ResolvePython("", getenv, lookPath, probe); got.Argv[0] != "/usr/bin/python3" {
		t.Fatalf("python3 must win over python: %+v", got)
	}
	t.Run("a python3 that resolves but does not run is skipped", func(t *testing.T) {
		// Verifier finding V2-2: on a stock Windows box exec.LookPath("python3")
		// is the Store alias; §5's order must fall through to the working
		// `python` instead of choosing the stub because it came first.
		found["python3"] = storeStub
		got, err := reconcile.ResolvePython("", getenv, lookPath, probe)
		if err != nil {
			t.Fatalf("the stub must be skipped, not chosen: %v", err)
		}
		if got.Argv[0] != "/usr/bin/python" || got.Source != "python on PATH" {
			t.Fatalf("got %+v, want python on PATH", got)
		}
	})
	t.Run("nothing found is a refusal naming every candidate and the stub", func(t *testing.T) {
		found = map[string]string{"python3": storeStub}
		_, err := reconcile.ResolvePython("", getenv, lookPath, probe)
		if err == nil {
			t.Fatal("nothing found must be a refusal")
		}
		f, ok := err.(*ui.Failure)
		if !ok {
			t.Fatalf("%T", err)
		}
		mustContain(t, f.Summary, "--python", "$"+reconcile.PythonEnvVar, "$VIRTUAL_ENV", "python3", "python")
		// R1: the skipped stub is named, with the reason it was skipped, so
		// the operator knows why python3 "on PATH" was not used.
		mustContain(t, f.Detail, storeStub, "9009", "does not run")
	})
}

// The embedded collector is the Python CLI's literal, verbatim, for as long
// as both exist (§1.3; P4 deletes cli/lazyaf and this test with it).
func TestPluginIsTheVerbatimPythonLiteral(t *testing.T) {
	path := filepath.Join("..", "..", "lazyaf", "cli.py")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%s is gone (%v); the cutover landed - delete this test with it", path, err)
	}
	const open = "_COLLECT_PLUGIN_SOURCE = '''"
	src := string(raw)
	start := strings.Index(src, open)
	if start < 0 {
		t.Fatalf("%s no longer defines _COLLECT_PLUGIN_SOURCE", path)
	}
	rest := src[start+len(open):]
	end := strings.Index(rest, "'''")
	if end < 0 {
		t.Fatal("unterminated literal")
	}
	if rest[:end] != reconcile.PluginSource() {
		t.Fatalf("lazyaf_collect_plugin.py differs from cli.py's _COLLECT_PLUGIN_SOURCE; copy it verbatim")
	}
}
