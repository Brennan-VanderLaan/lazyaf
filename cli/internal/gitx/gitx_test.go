package gitx

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"strings"
	"testing"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

func capture(t *testing.T) *bytes.Buffer {
	t.Helper()
	stderr := &bytes.Buffer{}
	savedOut, savedErr := ui.Stdout, ui.Stderr
	ui.Stdout, ui.Stderr = &bytes.Buffer{}, stderr
	t.Setenv("NO_COLOR", "1")
	t.Cleanup(func() { ui.Stdout, ui.Stderr = savedOut, savedErr })
	return stderr
}

// The recorder is the seam TestLand stands on: what it records must be
// exactly what was asked, in order.
func TestRecorderKeepsEveryCallInOrder(t *testing.T) {
	rec := &Recorder{Respond: func(c Call) (Result, error) {
		if c.Name == "gh" {
			return Result{ExitCode: 1, Stderr: "no GitHub host configured"}, nil
		}
		return Result{Stdout: "main\ndev\n"}, nil
	}}
	ctx := context.Background()
	if got := LocalBranches(ctx, rec, "/repo"); strings.Join(got, ",") != "main,dev" {
		t.Fatalf("LocalBranches = %v", got)
	}
	res, err := rec.Run(ctx, "/repo", "gh", "pr", "create", "--fill")
	if err != nil || res.ExitCode != 1 {
		t.Fatalf("gh: %+v %v", res, err)
	}
	calls := rec.Calls()
	if len(calls) != 2 || calls[0].Name != "git" || calls[1].Name != "gh" || calls[1].Dir != "/repo" {
		t.Fatalf("calls = %+v", calls)
	}
	if got := rec.CallsTo("gh"); len(got) != 1 || strings.Join(got[0], " ") != "pr create --fill" {
		t.Fatalf("CallsTo(gh) = %v", got)
	}
}

// git's own words survive the round trip through the refusal, brackets
// and all (the [rejected] defect, test_cli_errors.py:188).
func TestGitOrFailQuotesGitVerbatim(t *testing.T) {
	stderr := capture(t)
	rec := &Recorder{Respond: func(c Call) (Result, error) {
		return Result{ExitCode: 1, Stderr: " ! [rejected]        main -> main (non-fast-forward)\r\n"}, nil
	}}
	_, err := GitOrFail(context.Background(), rec, "/repo", []string{"push", "origin", "main"}, "could not push", "    git push origin main")
	var f *ui.Failure
	if !errors.As(err, &f) {
		t.Fatalf("GitOrFail returned %T, want *ui.Failure", err)
	}
	ui.Report(err)
	text := stderr.String()
	for _, want := range []string{"Error: could not push", "$ git push origin main", "[rejected]", "    git push origin main"} {
		if !strings.Contains(text, want) {
			t.Fatalf("refusal lacks %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "\r") {
		t.Fatalf("CRLF reached the quotation: %q", text)
	}
}

// A git that cannot be started is a refusal naming the install, not a Go
// error string the operator has to decode.
func TestGitOrFailWhenGitCannotStart(t *testing.T) {
	stderr := capture(t)
	rec := &Recorder{Respond: func(c Call) (Result, error) {
		return Result{}, &exec.Error{Name: "git", Err: exec.ErrNotFound}
	}}
	_, err := GitOrFail(context.Background(), rec, "", []string{"status"}, "could not run git", "")
	if err == nil {
		t.Fatal("expected a refusal")
	}
	ui.Report(err)
	if !strings.Contains(stderr.String(), "git-scm.com") {
		t.Fatalf("the remedy must name where git comes from:\n%s", stderr.String())
	}
}

// The real runner against the real git: exit codes are carried, not raised.
// git is a stated dependency of ingest/land (cli/README.md), so its absence
// here is a failure naming it, never a skip (R4).
func TestExecCarriesTheExitCode(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Fatalf("git is not on PATH (%v); install it - ingest and land need it and so does this test", err)
	}
	dir := t.TempDir()
	var real Exec
	res, err := real.Run(context.Background(), dir, "git", "rev-parse", "--is-inside-work-tree")
	if err != nil {
		t.Fatalf("git could not be run: %v", err)
	}
	if res.ExitCode == 0 {
		t.Fatalf("an empty temp dir is not a work tree; git exited 0 with %q", res.Stdout)
	}
	if !strings.Contains(res.Stderr, "not a git repository") {
		t.Fatalf("stderr = %q", res.Stderr)
	}
	if _, err := real.Run(context.Background(), dir, "definitely-not-a-program-xyz"); err == nil {
		t.Fatal("a program that does not exist must be the error return")
	}
}

func TestLinesDropsBlanks(t *testing.T) {
	if got := Lines("  a \n\n b\r\n"); strings.Join(got, ",") != "a,b" {
		t.Fatalf("Lines = %v", got)
	}
}
