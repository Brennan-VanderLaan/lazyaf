package cmd

import (
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/gitx"
)

// tdd/unit/scripts/test_cli_errors.py::TestUsageErrorsCarryAnExample
func TestUsageErrorsCarryAnExample(t *testing.T) {
	t.Run("ingest_without_name_names_the_flag_and_shows_a_command", func(t *testing.T) {
		// The owner's exact report: click named the missing flag and pointed
		// at --help; naming the flag is not the same as showing the command
		// that works.
		o := run(t, nil, "ingest", ".")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2 (usage)", o.code)
		}
		mustContain(t, o.all(), "--name", "lazyaf ingest ./my-project --name my-project")
	})
	t.Run("land_without_branch_shows_a_command", func(t *testing.T) {
		o := run(t, nil, "land", "abc123")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.all(), "--branch", "lazyaf land abc123 --branch feature/new-api")
	})
	t.Run("a_missing_positional_shows_a_command_too", func(t *testing.T) {
		o := run(t, nil, "branches")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.all(), "lazyaf branches abc123")
	})
	t.Run("the_example_comes_from_the_commands_own_help", func(t *testing.T) {
		// R3: one source. The example after a mistake is the example in
		// --help, so it cannot rot on the path nobody reads.
		root := NewRootWith(fakeDeps(t))
		ingest, _, err := root.Find([]string{"ingest"})
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(usageRemedy(ingest), ingest.Example) {
			t.Fatalf("the usage remedy is not the command's Example:\n%s", usageRemedy(ingest))
		}
		help := run(t, nil, "ingest", "--help")
		for _, line := range strings.Split(ingest.Example, "\n") {
			mustContain(t, help.stdout, strings.TrimSpace(line))
		}
	})
	t.Run("every_command_documents_a_runnable_example", func(t *testing.T) {
		// The mechanism above can only show what the commands carry.
		root := NewRootWith(fakeDeps(t))
		seen := 0
		var walk func(c *cobra.Command)
		walk = func(c *cobra.Command) {
			if c.Hidden { // cobra's own __complete
				return
			}
			seen++
			if strings.TrimSpace(c.Example) == "" {
				t.Errorf("`%s` documents no example", c.CommandPath())
			}
			for _, line := range strings.Split(c.Example, "\n") {
				line = strings.TrimSpace(line)
				if line == "" {
					continue
				}
				// A shell line that USES lazyaf (`source <(lazyaf completion
				// bash)`) is runnable too; every other line starts with it.
				if !strings.HasPrefix(line, "lazyaf ") && !strings.Contains(line, "lazyaf ") {
					t.Errorf("`%s`: example is not a runnable command line: %q", c.CommandPath(), line)
				}
			}
			for _, sub := range c.Commands() {
				walk(sub)
			}
		}
		walk(root)
		if seen < 17 { // root + ingest land list branches tests reconcile debug x8 completion help
			t.Fatalf("walked only %d commands; the tree has shrunk", seen)
		}
	})
	t.Run("a_mistyped_command_suggests_the_real_one", func(t *testing.T) {
		o := run(t, nil, "ingets", ".")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.all(), "Did you mean", "ingest")
		o = run(t, nil, "lst")
		mustContain(t, o.all(), "Did you mean", "list")
		// The same spelling help on a nested group (cobra only does the root).
		o = run(t, nil, "debug", "resmue", "s1")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.all(), "Did you mean", "resume", "lazyaf debug rerun")
	})
	t.Run("a_wholly_unknown_command_does_not_invent_a_suggestion", func(t *testing.T) {
		o := run(t, nil, "zzzzzzzz")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustNotContain(t, o.all(), "Did you mean")
		o = run(t, nil, "debug", "zzzzzzzz")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustNotContain(t, o.all(), "Did you mean")
	})
	t.Run("a_bare_group_is_not_a_success", func(t *testing.T) {
		// cobra would print help and exit 0; a script reading that as
		// "done" is the fake green R4 exists for.
		o := run(t, nil, "debug")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.stdout, "rerun", "attach")
	})
	t.Run("an_unknown_flag_is_a_usage_error_with_the_example", func(t *testing.T) {
		o := run(t, nil, "list", "--bogus")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.all(), "bogus", "lazyaf list")
	})
}

// gitRepoDir is the `_git_repo` fixture (test_cli_errors.py:744): a directory
// shaped like a git repo, with the recorder answering for git.
func gitRepoDir(t *testing.T) string {
	t.Helper()
	repo := filepath.Join(t.TempDir(), "repo")
	if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	return repo
}

// branchesRecorder controls what `git for-each-ref` reports.
func branchesRecorder(names ...string) *gitx.Recorder {
	return &gitx.Recorder{Respond: func(c gitx.Call) (gitx.Result, error) {
		if c.Name == "git" && len(c.Args) > 0 && c.Args[0] == "for-each-ref" {
			return gitx.Result{Stdout: strings.Join(names, "\n") + "\n"}, nil
		}
		return gitx.Result{}, nil
	}}
}

// tdd/unit/scripts/test_cli_errors.py::TestIngestRefusesEarly
func TestIngestRefusesEarly(t *testing.T) {
	t.Run("a_blank_name_is_refused_here_not_by_the_api", func(t *testing.T) {
		// `--name "   "` got past click and came back as a raw 422.
		deps := fakeDeps(t)
		deps.Git = branchesRecorder("main", "dev")
		o := run(t, deps, "--server", noHTTP(t), "ingest", gitRepoDir(t), "--name", "   ")
		if o.code == 0 {
			t.Fatal("a blank name exited 0")
		}
		mustContain(t, o.all(), "--name is empty", "lazyaf ingest")
	})
	t.Run("a_nonexistent_branch_is_refused_before_the_repo_is_created", func(t *testing.T) {
		// This used to create a repo record, then fail on push - leaving an
		// empty repo in LazyAF for every typo.
		deps := fakeDeps(t)
		deps.Git = branchesRecorder("main", "dev")
		o := run(t, deps, "--server", noHTTP(t), "ingest", gitRepoDir(t), "--name", "x", "--branch", "mian")
		if o.code == 0 {
			t.Fatal("a nonexistent branch exited 0")
		}
		mustContain(t, o.all(), "does not exist")
	})
	t.Run("the_branch_refusal_lists_the_branches_that_do_exist", func(t *testing.T) {
		deps := fakeDeps(t)
		deps.Git = branchesRecorder("main", "dev")
		o := run(t, deps, "--server", noHTTP(t), "ingest", gitRepoDir(t), "--name", "x", "--branch", "mian")
		mustContain(t, o.all(), "main", "dev")
	})
	t.Run("a_path_that_is_not_a_git_repo_names_the_remedy", func(t *testing.T) {
		plain := filepath.Join(t.TempDir(), "plain")
		if err := os.Mkdir(plain, 0o755); err != nil {
			t.Fatal(err)
		}
		o := run(t, nil, "--server", noHTTP(t), "ingest", plain, "--name", "x")
		if o.code == 0 {
			t.Fatal("a non-repo exited 0")
		}
		mustContain(t, o.all(), "not a git repository", "git init")
	})
	t.Run("a_path_that_does_not_exist_is_a_usage_error", func(t *testing.T) {
		o := run(t, nil, "--server", noHTTP(t), "ingest", filepath.Join(t.TempDir(), "nope"), "--name", "x")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2", o.code)
		}
		mustContain(t, o.all(), "does not exist", "lazyaf ingest ./my-project --name my-project")
	})
}

// tdd/unit/scripts/test_cli_errors.py::TestNothingExitsZeroOnFailure, the
// ingest half (the fail() half is ui.TestFailNeverExitsZero). A REAL `git
// init` in t.TempDir(): `git push --all` on an unborn HEAD succeeds and
// sends nothing, so this used to print a green Success! and exit 0.
func TestIngestRefusesNoCommits(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Fatalf("git is not on PATH (%v); ingest needs it and so does this test", err)
	}
	repo := filepath.Join(t.TempDir(), "empty")
	if err := os.Mkdir(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("git", "-C", repo, "init", "-q").CombinedOutput(); err != nil {
		t.Fatalf("git init: %v\n%s", err, out)
	}
	deps := fakeDeps(t)
	deps.Git = gitx.Exec{}

	t.Run("ingest_refuses_a_repo_with_no_commits", func(t *testing.T) {
		o := run(t, deps, "--server", noHTTP(t), "ingest", repo, "--name", "x", "--all-branches")
		if o.code == 0 {
			t.Fatal("an empty repo exited 0")
		}
		mustContain(t, o.all(), "no commits")
	})
	t.Run("the_no_commits_refusal_names_the_two_commands_that_fix_it", func(t *testing.T) {
		o := run(t, deps, "--server", noHTTP(t), "ingest", repo, "--name", "x")
		mustContain(t, o.all(), "git add -A", "git commit")
	})
}

// landing is the TestLand fixture (test_cli_errors.py:821-846): a `land` that
// gets as far as pushing, with every seam recorded. gh's answer is set by
// the test.
func landing(t *testing.T, gh func() (gitx.Result, error)) (*Deps, *gitx.Recorder, string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(dir)
	rec := &gitx.Recorder{Respond: func(c gitx.Call) (gitx.Result, error) {
		if c.Name == "gh" && gh != nil {
			return gh()
		}
		return gitx.Result{}, nil
	}}
	remote := "git@github.com:o/r.git"
	api := newFakeAPI(t, map[string]func([]byte) (int, any){
		"GET /api/repos/abc":           ok(map[string]any{"id": "abc", "name": "r", "remote_url": remote, "default_branch": "main"}),
		"GET /api/repos/abc/clone-url": ok(map[string]any{"clone_url": "http://h/git/abc.git"}),
	})
	deps := fakeDeps(t)
	deps.Git = rec
	return deps, rec, api.URL
}

// tdd/unit/scripts/test_cli_errors.py::TestLand
func TestLand(t *testing.T) {
	t.Run("the_push_refspec_is_fully_qualified_on_both_sides", func(t *testing.T) {
		// `lazyaf/<branch>:<branch>` fails whenever the destination branch
		// does not exist yet - i.e. every time an agent branch is landed for
		// the first time. git: "Neither worked, so we gave up. You must
		// fully qualify the ref."
		deps, rec, url := landing(t, nil)
		o := run(t, deps, "--server", url, "land", "abc", "--branch", "agent/fix", "--remote", "origin")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		var pushes [][]string
		for _, args := range rec.CallsTo("git") {
			if len(args) > 0 && args[0] == "push" {
				pushes = append(pushes, args)
			}
		}
		want := "push origin refs/remotes/lazyaf/agent/fix:refs/heads/agent/fix"
		if len(pushes) != 1 || strings.Join(pushes[0], " ") != want {
			t.Fatalf("pushes = %v, want exactly [%s]", pushes, want)
		}
	})
	t.Run("a_failed_pr_does_not_report_success", func(t *testing.T) {
		// Was: "Warning: PR creation failed", then a green "Landed!", exit 0
		// - a script above it saw the whole request honoured.
		deps, _, url := landing(t, func() (gitx.Result, error) {
			return gitx.Result{ExitCode: 1, Stderr: "no GitHub host configured"}, nil
		})
		o := run(t, deps, "--server", url, "land", "abc", "--branch", "agent/fix", "--pr")
		if o.code == 0 {
			t.Fatal("a failed PR exited 0")
		}
		mustContain(t, o.all(), "no GitHub host configured", "was pushed to", "does not need repeating", "gh pr create")
		mustNotContain(t, o.stdout, "Landed!")
	})
	t.Run("a_successful_pr_still_reports_success", func(t *testing.T) {
		deps, rec, url := landing(t, func() (gitx.Result, error) {
			return gitx.Result{Stdout: "https://github.com/o/r/pull/7\n"}, nil
		})
		o := run(t, deps, "--server", url, "land", "abc", "--branch", "agent/fix", "--pr")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "https://github.com/o/r/pull/7", "Landed!")
		gh := rec.CallsTo("gh")
		if len(gh) != 1 || strings.Join(gh[0], " ") != "pr create --base main --head agent/fix --fill" {
			t.Fatalf("gh calls = %v", gh)
		}
	})
	t.Run("running_outside_a_clone_names_the_remedy", func(t *testing.T) {
		t.Chdir(t.TempDir()) // no .git here
		api := newFakeAPI(t, map[string]func([]byte) (int, any){
			"GET /api/repos/abc":           ok(map[string]any{"id": "abc", "name": "r", "remote_url": nil, "default_branch": "main"}),
			"GET /api/repos/abc/clone-url": ok(map[string]any{"clone_url": "http://h/git/abc.git"}),
		})
		o := run(t, nil, "--server", api.URL, "land", "abc", "--branch", "agent/fix")
		if o.code == 0 {
			t.Fatal("outside a clone exited 0")
		}
		mustContain(t, o.all(), "not a git repository", "cd /path/to/your/clone")
	})
	t.Run("an_unknown_repo_uses_the_commands_own_words", func(t *testing.T) {
		deps, _, _ := landing(t, nil)
		api := newFakeAPI(t, map[string]func([]byte) (int, any){})
		o := run(t, deps, "--server", api.URL, "land", "nope", "--branch", "b")
		if o.code == 0 {
			t.Fatal("a 404 exited 0")
		}
		mustContain(t, o.all(), "repo nope does not exist", "lazyaf list")
	})
}

// upcoming/go-cli.md §4.3: `--version` with $LAZYAF_SERVER pointed at a
// listener that counts connections asserts zero dials - the version is a
// fact about the binary, not about any backend.
func TestVersionNeverDials(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	var dials atomic.Int32
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			dials.Add(1)
			_ = conn.Close()
		}
	}()
	t.Setenv("LAZYAF_SERVER", "http://"+l.Addr().String())
	for _, args := range [][]string{{"--version"}, {"--version", "--short"}} {
		o := run(t, nil, args...)
		if o.code != 0 || strings.TrimSpace(o.stdout) == "" {
			t.Fatalf("%v: exit %d, stdout %q, stderr %q", args, o.code, o.stdout, o.stderr)
		}
	}
	if n := dials.Load(); n != 0 {
		t.Fatalf("--version dialed the backend %d time(s)", n)
	}
	// And the plumbing the assertion rests on really counts a dial.
	if _, err := http.Get("http://" + l.Addr().String()); err == nil || dials.Load() != 1 {
		t.Fatalf("the dial counter did not see a real connection (dials=%d, err=%v)", dials.Load(), err)
	}
}

// The persistent --server flag is accepted in the Python CLI's position -
// after the subcommand - so existing scripts do not change (§5).
func TestServerFlagIsAcceptedAfterTheSubcommand(t *testing.T) {
	api := newFakeAPI(t, map[string]func([]byte) (int, any){
		"GET /api/repos": ok([]any{}),
	})
	o := run(t, nil, "list", "--server", api.URL)
	if o.code != 0 {
		t.Fatalf("exit %d:\n%s", o.code, o.all())
	}
	mustContain(t, o.stdout, "No repos found", "(from --server)")
	o = run(t, nil, "list", "-s", api.URL)
	if o.code != 0 {
		t.Fatalf("exit %d:\n%s", o.code, o.all())
	}
	if len(api.Calls()) != 2 {
		t.Fatalf("calls = %v", api.Calls())
	}
}

// The commands that only read: their output shapes, on stdout, against the
// real wire shapes (routers/repos.py:372-385).
func TestListAndBranches(t *testing.T) {
	api := newFakeAPI(t, map[string]func([]byte) (int, any){
		"GET /api/repos": ok([]any{
			map[string]any{"id": "abc", "name": "demo", "is_ingested": true, "remote_url": "git@h:o/r.git"},
			map[string]any{"id": "def", "name": "other", "is_ingested": false, "remote_url": nil},
		}),
		"GET /api/repos/abc/branches": ok(map[string]any{
			"branches": []any{
				map[string]any{"name": "main", "commit": "0123456789abcdef", "is_default": true, "is_lazyaf": false},
				map[string]any{"name": "lazyaf/x", "commit": "fedcba9876543210", "is_default": false, "is_lazyaf": true},
			},
			"default_branch": "main", "total": 2,
		}),
		"GET /api/repos/empty/branches": ok(map[string]any{"branches": []any{}, "default_branch": "main", "total": 0}),
	})
	t.Run("list", func(t *testing.T) {
		o := run(t, nil, "--server", api.URL, "list")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "Found 2 repo(s)", "  abc  demo  ingested", "    Remote: git@h:o/r.git", "  def  other  not ingested")
		if o.stderr != "" {
			t.Fatalf("results leaked to stderr: %q", o.stderr)
		}
	})
	t.Run("branches", func(t *testing.T) {
		o := run(t, nil, "--server", api.URL, "branches", "abc")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "Branches in repo (2)", "  main  01234567  default", "  lazyaf/x  fedcba98  lazyaf")
	})
	t.Run("branches of an empty repo names the fix", func(t *testing.T) {
		o := run(t, nil, "--server", api.URL, "branches", "empty")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "no branches yet", "lazyaf ingest <path> --name <name>")
	})
	t.Run("branches of an unknown repo", func(t *testing.T) {
		o := run(t, nil, "--server", api.URL, "branches", "nope")
		if o.code == 0 {
			t.Fatal("a 404 exited 0")
		}
		mustContain(t, o.stderr, "repo nope does not exist", "lazyaf list")
	})
}

// The whole ingest path against a recorder and a fake backend: the detected
// branch is what gets sent (never a hard-coded "main"), the lazyaf remote is
// (re)added, the push is the branch, and success is only reported once the
// server says it has branches.
func TestIngestHappyPath(t *testing.T) {
	repo := gitRepoDir(t)
	rec := &gitx.Recorder{Respond: func(c gitx.Call) (gitx.Result, error) {
		switch strings.Join(c.Args, " ") {
		case "for-each-ref --format=%(refname:short) refs/heads":
			return gitx.Result{Stdout: "master\n"}, nil
		case "rev-parse --abbrev-ref HEAD":
			return gitx.Result{Stdout: "master\n"}, nil
		case "remote get-url origin":
			return gitx.Result{ExitCode: 2, Stderr: "error: No such remote 'origin'"}, nil
		}
		return gitx.Result{}, nil
	}}
	var posted string
	api := newFakeAPI(t, map[string]func([]byte) (int, any){
		"POST /api/repos/ingest": func(body []byte) (int, any) {
			posted = string(body)
			return 201, map[string]any{"id": "r1", "name": "demo", "internal_git_url": "/git/r1.git", "clone_url": "http://h/git/r1.git"}
		},
		"GET /api/repos/r1/branches": ok(map[string]any{
			"branches":       []any{map[string]any{"name": "master", "commit": "abc", "is_default": true, "is_lazyaf": false}},
			"default_branch": "master", "total": 1,
		}),
	})
	deps := fakeDeps(t)
	deps.Git = rec
	o := run(t, deps, "--server", api.URL, "ingest", repo, "--name", "demo")
	if o.code != 0 {
		t.Fatalf("exit %d:\n%s", o.code, o.all())
	}
	mustContain(t, posted, `"default_branch":"master"`, `"name":"demo"`)
	mustNotContain(t, posted, "remote_url")
	mustContain(t, o.stdout, "Using current branch as the default: master", "Created repo r1", "Success!", "Repo ID: r1", "Branches on the server: master", "lazyaf branches r1")
	git := rec.CallsTo("git")
	joined := make([]string, 0, len(git))
	for _, args := range git {
		joined = append(joined, strings.Join(args, " "))
	}
	mustContain(t, strings.Join(joined, "\n"), "remote remove lazyaf", "remote add lazyaf http://h/git/r1.git", "push lazyaf master")
	if api.Calls()[0] != "POST /api/repos/ingest" || api.Calls()[1] != "GET /api/repos/r1/branches" {
		t.Fatalf("calls = %v", api.Calls())
	}
}

// A push that "succeeds" while the server still has nothing is a refusal,
// never a Success! (cli.py:626-640).
func TestIngestRefusesWhenTheServerStillHasNoBranches(t *testing.T) {
	repo := gitRepoDir(t)
	api := newFakeAPI(t, map[string]func([]byte) (int, any){
		"POST /api/repos/ingest":     func([]byte) (int, any) { return 201, map[string]any{"id": "r1", "clone_url": "http://h/git/r1.git"} },
		"GET /api/repos/r1/branches": ok(map[string]any{"branches": []any{}, "default_branch": "main", "total": 0}),
	})
	deps := fakeDeps(t)
	deps.Git = branchesRecorder("main")
	o := run(t, deps, "--server", api.URL, "ingest", repo, "--name", "demo", "--branch", "main")
	if o.code == 0 {
		t.Fatal("an empty server-side repo exited 0")
	}
	mustContain(t, o.stderr, "still has no branches", "git -C")
	mustNotContain(t, o.stdout, "Success!")
}
