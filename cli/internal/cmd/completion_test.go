package cmd

import (
	"os/exec"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

// upcoming/go-cli.md §7 tests.

// complete runs the hidden `__complete` protocol and returns the candidate
// lines and the directive line (":N").
func complete(t *testing.T, env map[string]string, words ...string) (candidates []string, directive string) {
	t.Helper()
	for k, v := range env {
		t.Setenv(k, v)
	}
	o := run(t, nil, append([]string{cobra.ShellCompRequestCmd}, words...)...)
	if o.code != 0 {
		t.Fatalf("__complete exited %d:\n%s", o.code, o.all())
	}
	for _, line := range strings.Split(strings.TrimRight(o.stdout, "\n"), "\n") {
		if strings.HasPrefix(line, ":") {
			directive = line
			continue
		}
		if line != "" {
			candidates = append(candidates, line)
		}
	}
	return candidates, directive
}

func names(candidates []string) []string {
	var out []string
	for _, c := range candidates {
		out = append(out, strings.SplitN(c, "\t", 2)[0])
	}
	return out
}

func TestCompletionScriptsParse(t *testing.T) {
	scripts := map[string]string{}
	for _, shell := range Shells {
		o := run(t, nil, "completion", shell)
		if o.code != 0 || o.stdout == "" {
			t.Fatalf("completion %s: exit %d, %d bytes:\n%s", shell, o.code, len(o.stdout), o.stderr)
		}
		scripts[shell] = o.stdout
	}
	if !strings.HasPrefix(scripts["zsh"], "#compdef lazyaf") {
		t.Fatalf("the zsh script does not start with #compdef lazyaf:\n%.80s", scripts["zsh"])
	}
	if !strings.Contains(scripts["fish"], "lazyaf") || !strings.Contains(scripts["powershell"], "lazyaf") {
		t.Fatal("the fish or powershell script does not name lazyaf")
	}
	// bash -n parses the bash script. bash is on the TG host (the tier image
	// is Debian; the owner's box has Git Bash); its absence is a failure
	// naming it, never a skip (R4).
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Fatalf("bash is not on PATH (%v); TestCompletionScriptsParse needs it to syntax-check the bash script", err)
	}
	cmd := exec.Command(bash, "-n")
	cmd.Stdin = strings.NewReader(scripts["bash"])
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("bash -n rejected the completion script: %v\n%s", err, out)
	}
	t.Run("a bad shell is a usage error listing the four", func(t *testing.T) {
		o := run(t, nil, "completion", "tcsh")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.stderr, "bash, zsh, fish, powershell", "lazyaf completion bash")
		if o := run(t, nil, "completion"); o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
	})
}

// Every leaf is reachable through the completion mechanism. Stated
// deviation from the §7 wording "appears in the bash and zsh output":
// cobra's V2 bash script and its zsh script embed NO command names - both
// delegate every word to `lazyaf __complete`, which is the whole point of
// V2 (the tree cannot drift from the script). So the property is asserted
// where it lives: `__complete <parent...> ""` must list every leaf, and
// `__complete debug ""` the seven verbs.
func TestEveryLeafCommandIsCompletable(t *testing.T) {
	root := NewRootWith(fakeDeps(t))
	var leaves [][]string
	var walk func(c *cobra.Command, path []string)
	walk = func(c *cobra.Command, path []string) {
		if c.Hidden {
			return
		}
		if len(c.Commands()) == 0 {
			leaves = append(leaves, append(append([]string(nil), path...), c.Name()))
			return
		}
		for _, sub := range c.Commands() {
			walk(sub, append(append([]string(nil), path...), c.Name()))
		}
	}
	for _, sub := range root.Commands() {
		walk(sub, nil)
	}
	// The §5 command table, spelled out. A `>= N` floor let `init` and
	// `doctor` go missing from the tree while this test stayed green
	// (verifier finding V4-1); set equality catches a dropped leaf AND an
	// unlisted new one, so the table and the tree move together.
	// `help` is cobra's own command, kept visible and given an Example in
	// root.go, so it is a leaf too.
	wantLeaves := []string{
		"ingest", "land", "list", "branches",
		"tests reconcile",
		"debug rerun", "debug list", "debug status", "debug attach",
		"debug resume", "debug abort", "debug extend",
		"init", "doctor", "completion", "help",
	}
	var gotLeaves []string
	for _, leaf := range leaves {
		gotLeaves = append(gotLeaves, strings.Join(leaf, " "))
	}
	sort.Strings(wantLeaves)
	sort.Strings(gotLeaves)
	if strings.Join(gotLeaves, "|") != strings.Join(wantLeaves, "|") {
		t.Fatalf("leaf commands in the tree do not match the §5 table:\n got %v\nwant %v", gotLeaves, wantLeaves)
	}
	for _, leaf := range leaves {
		parent, name := leaf[:len(leaf)-1], leaf[len(leaf)-1]
		got, _ := complete(t, map[string]string{NoRemoteEnvVar: "1"}, append(append([]string(nil), parent...), "")...)
		found := false
		for _, n := range names(got) {
			if n == name {
				found = true
			}
		}
		if !found {
			t.Errorf("`lazyaf %s` is not offered by `__complete %s \"\"`: %v", strings.Join(leaf, " "), strings.Join(parent, " "), names(got))
		}
	}
	got, directive := complete(t, nil, "debug", "")
	want := []string{"abort", "attach", "extend", "list", "rerun", "resume", "status"}
	if strings.Join(names(got), " ") != strings.Join(want, " ") {
		t.Fatalf("__complete debug \"\" = %v, want %v", names(got), want)
	}
	if directive != ":4" { // ShellCompDirectiveNoFileComp
		t.Fatalf("directive = %q, want :4", directive)
	}
	// Descriptions ride along for the shells that show them.
	if !strings.Contains(got[0], "\t") {
		t.Fatalf("no description on %q", got[0])
	}
}

// `__complete land ""` against an httptest.Server speaking the real
// /api/repos shape: ids with names as descriptions, NoFileComp.
func TestDynamicCompletion(t *testing.T) {
	api := newFakeAPI(t, map[string]func([]byte) (int, any){
		"GET /api/repos": ok([]any{
			map[string]any{"id": "abc123", "name": "demo", "is_ingested": true},
			map[string]any{"id": "def456", "name": "other", "is_ingested": false},
		}),
		"GET /api/repos/abc123/branches": ok(map[string]any{
			"branches": []any{
				map[string]any{"name": "main", "commit": "0", "is_default": true, "is_lazyaf": false},
				map[string]any{"name": "lazyaf/x", "commit": "1", "is_default": false, "is_lazyaf": true},
			}, "default_branch": "main", "total": 2,
		}),
		"GET /api/debug": ok([]any{
			map[string]any{"id": "sess-1", "status": "waiting_at_breakpoint", "current_step": map[string]any{"key": "verify", "name": "verify", "index": 1, "type": "script"},
				"breakpoints": []string{}, "breakpoints_hit": []string{}, "breakpoints_pending": []string{}, "attach_available": true,
				"commit": map[string]any{"sha": "a", "message": "m", "branch": "b"}, "runtime": map[string]any{"host": "local", "orchestrator": "d", "image": "i"}},
		}),
	})
	env := map[string]string{"LAZYAF_SERVER": api.URL, NoRemoteEnvVar: ""}
	t.Run("repo ids for land branches and tests reconcile", func(t *testing.T) {
		for _, words := range [][]string{{"land", ""}, {"branches", ""}, {"tests", "reconcile", ""}} {
			got, directive := complete(t, env, words...)
			if strings.Join(got, "|") != "abc123\tdemo|def456\tother" || directive != ":4" {
				t.Fatalf("__complete %v = %v %s", words, got, directive)
			}
		}
		got, _ := complete(t, env, "land", "d")
		if strings.Join(names(got), " ") != "def456" {
			t.Fatalf("prefix filter: %v", got)
		}
	})
	t.Run("the flag on the line wins over the environment", func(t *testing.T) {
		got, directive := complete(t, map[string]string{"LAZYAF_SERVER": closedPort(t)}, "land", "--server", api.URL, "")
		if len(got) != 2 || directive != ":4" {
			t.Fatalf("__complete with --server = %v %s", got, directive)
		}
	})
	t.Run("session ids for every debug verb but rerun", func(t *testing.T) {
		for _, verb := range []string{"status", "attach", "resume", "abort", "extend"} {
			got, _ := complete(t, env, "debug", verb, "")
			if strings.Join(got, "|") != "sess-1\twaiting_at_breakpoint at verify" {
				t.Fatalf("__complete debug %s = %v", verb, got)
			}
		}
		got, _ := complete(t, env, "debug", "rerun", "")
		if len(got) != 0 {
			t.Fatalf("rerun has no listing endpoint and must offer nothing; got %v", got)
		}
	})
	t.Run("land --branch from the repo already on the line", func(t *testing.T) {
		got, _ := complete(t, env, "land", "abc123", "--branch", "")
		if strings.Join(got, " ") != "main lazyaf/x" {
			t.Fatalf("__complete land abc123 --branch = %v", got)
		}
		got, _ = complete(t, env, "land", "--branch", "")
		if len(got) != 0 {
			t.Fatalf("with no repo id on the line nothing can be looked up; got %v", got)
		}
	})
	t.Run("LAZYAF_COMPLETE_NO_REMOTE keeps TAB local", func(t *testing.T) {
		before := len(api.Calls())
		got, directive := complete(t, map[string]string{"LAZYAF_SERVER": api.URL, NoRemoteEnvVar: "1"}, "land", "")
		if len(got) != 0 || directive != ":4" || len(api.Calls()) != before {
			t.Fatalf("remote lookup ran with %s=1: %v %s (calls %d -> %d)", NoRemoteEnvVar, got, directive, before, len(api.Calls()))
		}
	})
}

// A dead backend costs at most the timeout and yields the error directive
// with zero candidates - never a hang, never a word on the command line.
func TestDynamicCompletionServerDown(t *testing.T) {
	start := time.Now()
	got, directive := complete(t, map[string]string{"LAZYAF_SERVER": closedPort(t), NoRemoteEnvVar: ""}, "land", "")
	elapsed := time.Since(start)
	if len(got) != 0 {
		t.Fatalf("candidates from a closed port: %v", got)
	}
	if directive != ":1" { // ShellCompDirectiveError
		t.Fatalf("directive = %q, want :1 (ShellCompDirectiveError)", directive)
	}
	if elapsed > RemoteTimeout+2*time.Second {
		t.Fatalf("a closed port took %s; the lookup must be bounded by %s", elapsed, RemoteTimeout)
	}
	// A schemeless $LAZYAF_SERVER is refused by api.New; on TAB that is the
	// same quiet zero, not a refusal printed into the command line.
	got, directive = complete(t, map[string]string{"LAZYAF_SERVER": "localhost:8000", NoRemoteEnvVar: ""}, "branches", "")
	if len(got) != 0 || directive != ":4" {
		t.Fatalf("schemeless server on TAB: %v %s", got, directive)
	}
}
