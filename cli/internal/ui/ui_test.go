package ui

import (
	"bytes"
	"errors"
	"strings"
	"testing"
)

// capture swaps both streams for buffers for one test.
func capture(t *testing.T) (stdout, stderr *bytes.Buffer) {
	t.Helper()
	stdout, stderr = &bytes.Buffer{}, &bytes.Buffer{}
	savedOut, savedErr := Stdout, Stderr
	Stdout, Stderr = stdout, stderr
	t.Setenv("NO_COLOR", "1")
	t.Cleanup(func() { Stdout, Stderr = savedOut, savedErr })
	return stdout, stderr
}

// The git output that exposed the original defect (test_cli_errors.py:188):
// rich deleted `[rejected]` as if it were a style tag.
const gitRejection = "To http://localhost:8000/git/abc\n" +
	" ! [rejected]        main -> main (non-fast-forward)\n" +
	"error: failed to push some refs to 'http://localhost:8000/git/abc'"

// tdd/unit/scripts/test_cli_errors.py::TestUntrustedTextIsNeverParsed
func TestVerbatimQuoting(t *testing.T) {
	t.Run("git stderr is quoted verbatim brackets and all", func(t *testing.T) {
		_, stderr := capture(t)
		Report(Fail("push failed", WithDetail(gitRejection)))
		if !strings.Contains(stderr.String(), "[rejected]") {
			t.Fatalf("the word naming the failure was dropped:\n%s", stderr.String())
		}
		if !strings.Contains(stderr.String(), "(non-fast-forward)") {
			t.Fatalf("git's explanation was not quoted:\n%s", stderr.String())
		}
	})
	t.Run("a closing tag shape does not crash the report", func(t *testing.T) {
		_, stderr := capture(t)
		Report(Fail("bad path", WithDetail("no such file [/tmp/x]")))
		if !strings.Contains(stderr.String(), "[/tmp/x]") {
			t.Fatalf("[/tmp/x] was interpreted:\n%s", stderr.String())
		}
	})
	t.Run("the summary line is not parsed either", func(t *testing.T) {
		// Summaries interpolate argv and server values, so they are text too.
		_, stderr := capture(t)
		Report(Fail("repo [weird] not found"))
		if !strings.Contains(stderr.String(), "Error: repo [weird] not found") {
			t.Fatalf("summary altered:\n%s", stderr.String())
		}
	})
	t.Run("the remedy is not parsed either", func(t *testing.T) {
		_, stderr := capture(t)
		Report(Fail("nope", WithRemedy("run: git log --format=[%h]")))
		if !strings.Contains(stderr.String(), "run: git log --format=[%h]") {
			t.Fatalf("remedy altered:\n%s", stderr.String())
		}
	})
	t.Run("windows line endings do not double space the quote", func(t *testing.T) {
		_, stderr := capture(t)
		Report(Fail("x", WithDetail("line one\r\nline two\r\n")))
		lines := strings.Split(strings.TrimRight(stderr.String(), "\n"), "\n")
		if len(lines) != 3 || lines[1] != "  line one" || lines[2] != "  line two" {
			t.Fatalf("quoted lines = %q, want exactly two indented lines", lines[1:])
		}
	})
	t.Run("an empty detail is not printed as a blank quotation", func(t *testing.T) {
		_, stderr := capture(t)
		Report(Fail("x", WithDetail("   \n\n")))
		if got := strings.TrimRight(stderr.String(), "\n"); got != "Error: x" {
			t.Fatalf("stderr = %q, want just the summary", got)
		}
	})
	t.Run("a percent sign in untrusted text is not a format verb", func(t *testing.T) {
		// fmt.Fprint, never Fprintf, on anything the CLI did not author.
		_, stderr := capture(t)
		Report(Fail("100% [done]", WithDetail("50%d of %s"), WithRemedy("%v")))
		for _, want := range []string{"100% [done]", "50%d of %s", "%v"} {
			if !strings.Contains(stderr.String(), want) {
				t.Fatalf("%q was interpreted:\n%s", want, stderr.String())
			}
		}
	})
}

// tdd/unit/scripts/test_cli_errors.py::TestNothingExitsZeroOnFailure
func TestFailNeverExitsZero(t *testing.T) {
	t.Run("fail refuses to be asked for a zero exit", func(t *testing.T) {
		defer func() {
			if recover() == nil {
				t.Fatal("WithExit(0) must panic - the guard is in the helper so no caller can reintroduce it")
			}
		}()
		Fail("boom", WithExit(0))
	})
	t.Run("fail exits with the code it was given", func(t *testing.T) {
		capture(t)
		for _, code := range []int{1, 2, 130} {
			if got := Report(Fail("boom", WithExit(code))); got != code {
				t.Fatalf("Report(Fail(WithExit(%d))) = %d", code, got)
			}
		}
	})
	t.Run("the default is 1 and Usage is 2", func(t *testing.T) {
		capture(t)
		if got := Report(Fail("boom")); got != ExitFailure {
			t.Fatalf("Report(Fail) = %d, want 1", got)
		}
		if got := Report(Usage("boom")); got != ExitUsage {
			t.Fatalf("Report(Usage) = %d, want 2", got)
		}
		if got := Report(Interrupted()); got != ExitInterrupt {
			t.Fatalf("Report(Interrupted) = %d, want 130", got)
		}
	})
	t.Run("a plain error is still a refusal not a zero", func(t *testing.T) {
		_, stderr := capture(t)
		if got := Report(errors.New("something unexpected")); got != ExitFailure {
			t.Fatalf("Report(plain error) = %d, want 1", got)
		}
		if !strings.Contains(stderr.String(), "Error: something unexpected") {
			t.Fatalf("stderr = %q", stderr.String())
		}
	})
	t.Run("a Failure literal without a code cannot exit 0", func(t *testing.T) {
		capture(t)
		if got := Report(&Failure{Summary: "forgot the code"}); got != ExitFailure {
			t.Fatalf("Report(&Failure{}) = %d, want 1", got)
		}
	})
	t.Run("nil is success", func(t *testing.T) {
		if got := Report(nil); got != ExitOK {
			t.Fatalf("Report(nil) = %d", got)
		}
	})
}

// tdd/unit/scripts/test_cli_errors.py::TestStreams
func TestStreams(t *testing.T) {
	t.Run("refusals go to stderr", func(t *testing.T) {
		stdout, stderr := capture(t)
		Report(Fail("nope", WithDetail("because"), WithRemedy("do this")))
		if stdout.Len() != 0 {
			t.Fatalf("a refusal reached stdout: %q", stdout.String())
		}
		for _, want := range []string{"Error: nope", "  because", "do this"} {
			if !strings.Contains(stderr.String(), want) {
				t.Fatalf("stderr lacks %q:\n%s", want, stderr.String())
			}
		}
	})
	t.Run("warnings go to stderr", func(t *testing.T) {
		stdout, stderr := capture(t)
		Warn("heads up", "detail [here]")
		Note("connecting to %s", "ws://x")
		if stdout.Len() != 0 {
			t.Fatalf("a warning reached stdout: %q", stdout.String())
		}
		if !strings.Contains(stderr.String(), "Warning: heads up") || !strings.Contains(stderr.String(), "  detail [here]") {
			t.Fatalf("stderr = %q", stderr.String())
		}
		if !strings.Contains(stderr.String(), "connecting to ws://x") {
			t.Fatalf("Note missing from stderr: %q", stderr.String())
		}
	})
	t.Run("results stay on stdout", func(t *testing.T) {
		stdout, stderr := capture(t)
		Out("  %s  %s", "abc", "demo")
		if stderr.Len() != 0 {
			t.Fatalf("a result reached stderr: %q", stderr.String())
		}
		if stdout.String() != "  abc  demo\n" {
			t.Fatalf("stdout = %q", stdout.String())
		}
	})
	t.Run("no ANSI codes when the stream is not a terminal", func(t *testing.T) {
		// NO_COLOR is set by capture; a buffer is not a tty either. Both
		// gates must hold for a byte of escape to appear.
		_, stderr := capture(t)
		Report(Fail("plain"))
		if strings.Contains(stderr.String(), "\x1b[") {
			t.Fatalf("ANSI escapes reached a non-tty stream: %q", stderr.String())
		}
	})
}
