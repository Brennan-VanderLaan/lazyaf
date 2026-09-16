// Package gitx runs git and gh as subprocesses behind a Runner seam.
//
// Port of cli/lazyaf/cli.py run_git / git_or_fail (:330-360) and the one
// `gh pr create` call in `land` (:783-784). The seam exists for one reason
// the Python tests state (test_cli_errors.py::TestLand): the push refspec
// `refs/remotes/lazyaf/<b>:refs/heads/<b>` is a fact about the ARGUMENTS
// handed to git, and the only honest way to pin it is to record them. Exec
// is the real runner; Recorder is the test one. Nothing in this package
// parses git's output beyond splitting lines - its stderr is quoted to the
// operator verbatim (ui.Verbatim), never interpreted.
package gitx

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"sync"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// Result is what one subprocess said and how it ended. A non-zero exit is
// carried here, not as an error: git saying "no" is an answer the caller
// quotes, not a failure of the runner.
type Result struct {
	Stdout   string
	Stderr   string
	ExitCode int
}

// Runner runs one program. The error return is for "could not run it at
// all" (not on PATH, cwd missing) - the one case with no exit code to show.
type Runner interface {
	Run(ctx context.Context, dir string, name string, args ...string) (Result, error)
}

// Exec is the real runner: os/exec, output captured, PATHEXT honoured on
// Windows through exec.LookPath (§12).
type Exec struct{}

// Run implements Runner.
func (Exec) Run(ctx context.Context, dir string, name string, args ...string) (Result, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	res := Result{Stdout: stdout.String(), Stderr: stderr.String()}
	var exitErr *exec.ExitError
	switch {
	case err == nil:
		return res, nil
	case errors.As(err, &exitErr):
		res.ExitCode = exitErr.ExitCode()
		return res, nil
	default:
		return res, err
	}
}

// Call is one recorded invocation.
type Call struct {
	Dir  string
	Name string
	Args []string
}

// Recorder is the test runner: every call is kept, and Respond decides what
// each one answers (nil answers exit 0 with empty output).
type Recorder struct {
	mu      sync.Mutex
	calls   []Call
	Respond func(c Call) (Result, error)
}

// Run implements Runner.
func (r *Recorder) Run(ctx context.Context, dir string, name string, args ...string) (Result, error) {
	call := Call{Dir: dir, Name: name, Args: append([]string(nil), args...)}
	r.mu.Lock()
	r.calls = append(r.calls, call)
	r.mu.Unlock()
	if r.Respond == nil {
		return Result{}, nil
	}
	return r.Respond(call)
}

// Calls is every invocation so far, in order.
func (r *Recorder) Calls() []Call {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Call(nil), r.calls...)
}

// CallsTo is every invocation of one program, as argv slices.
func (r *Recorder) CallsTo(name string) [][]string {
	var out [][]string
	for _, c := range r.Calls() {
		if c.Name == name {
			out = append(out, c.Args)
		}
	}
	return out
}

// Git runs one git command in dir.
func Git(ctx context.Context, r Runner, dir string, args ...string) (Result, error) {
	return r.Run(ctx, dir, "git", args...)
}

// GitOrFail runs git; on a non-zero exit it quotes git verbatim and returns
// a refusal (cli.py:340-360). git's stderr IS the diagnosis - "src refspec
// X does not match any", "! [rejected] ... (non-fast-forward)" - so it is
// reproduced exactly, including the bracketed words rich used to eat. A git
// that could not be started at all is the same refusal with that reason and
// the install remedy, never a Go stack trace.
func GitOrFail(ctx context.Context, r Runner, dir string, args []string, summary, remedy string) (Result, error) {
	res, err := Git(ctx, r, dir, args...)
	if err != nil {
		return res, ui.Fail(summary,
			ui.WithDetail(fmt.Sprintf("$ git %s\n%v", strings.Join(args, " "), err)),
			ui.WithRemedy("git could not be run. Install it and make sure `git` is on PATH:\n    https://git-scm.com/downloads"))
	}
	if res.ExitCode != 0 {
		detail := strings.TrimSpace(res.Stderr)
		if detail == "" {
			detail = strings.TrimSpace(res.Stdout)
		}
		opts := []ui.Option{ui.WithDetail(fmt.Sprintf("$ git %s\n%s", strings.Join(args, " "), detail))}
		if remedy != "" {
			opts = append(opts, ui.WithRemedy(remedy))
		}
		return res, ui.Fail(summary, opts...)
	}
	return res, nil
}

// LocalBranches is every local branch in dir. Empty means the repo has no
// commits (cli.py:451-459) - or that git could not be asked, which for
// "is there anything to push" is the same answer.
func LocalBranches(ctx context.Context, r Runner, dir string) []string {
	res, err := Git(ctx, r, dir, "for-each-ref", "--format=%(refname:short)", "refs/heads")
	if err != nil || res.ExitCode != 0 {
		return nil
	}
	return Lines(res.Stdout)
}

// CurrentBranch is `git rev-parse --abbrev-ref HEAD`, or an error carrying
// git's stderr when HEAD is detached or unborn.
func CurrentBranch(ctx context.Context, r Runner, dir string) (string, string, error) {
	res, err := Git(ctx, r, dir, "rev-parse", "--abbrev-ref", "HEAD")
	if err != nil {
		return "", "", err
	}
	name := strings.TrimSpace(res.Stdout)
	if res.ExitCode != 0 || name == "" {
		return "", strings.TrimSpace(res.Stderr), errors.New("no current branch")
	}
	return name, "", nil
}

// RemoteURL is `git remote get-url <name>`, or "" when there is none.
func RemoteURL(ctx context.Context, r Runner, dir, name string) string {
	res, err := Git(ctx, r, dir, "remote", "get-url", name)
	if err != nil || res.ExitCode != 0 {
		return ""
	}
	return strings.TrimSpace(res.Stdout)
}

// Remotes is `git remote` in dir, for --remote completion (§7).
func Remotes(ctx context.Context, r Runner, dir string) []string {
	res, err := Git(ctx, r, dir, "remote")
	if err != nil || res.ExitCode != 0 {
		return nil
	}
	return Lines(res.Stdout)
}

// Lines splits captured output into its non-blank, trimmed lines.
func Lines(text string) []string {
	var out []string
	for _, line := range strings.Split(text, "\n") {
		if s := strings.TrimSpace(line); s != "" {
			out = append(out, s)
		}
	}
	return out
}
