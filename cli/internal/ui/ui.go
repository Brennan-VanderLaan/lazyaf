// Package ui is how the binary talks to the operator.
//
// THE ERROR CONTRACT (ported from cli/lazyaf/cli.py:14-25 and :68-140):
//
//   - diagnostics on stderr, results on stdout, so `lazyaf list | ...` pipes
//     clean data;
//   - a failure never exits 0 (Fail panics on a zero code - the guard is in
//     the helper so no future caller can reintroduce it);
//   - text this CLI did not author - git's stderr, an API body - is written
//     with fmt.Fprint and never interpreted. There is NO markup parser in the
//     binary: rich read `[rejected]` as a style tag and deleted the one word
//     that explained a push failure (test_cli_errors.py:188). Colour, when a
//     tty wants it, wraps a string in ANSI codes and never parses it;
//   - a refusal names the remedy, not `--help`.
//
// Errors are values here: Fail builds a *Failure, Report prints it, and
// cmd.Execute turns it into the process exit code. One printer, so a
// refusal built in `api` or `terminal` reads exactly like one built in `cmd`.
package ui

import (
	"fmt"
	"io"
	"os"
	"strings"
)

// Exit codes (cli.py:56-59 + §5).
const (
	ExitOK        = 0
	ExitFailure   = 1
	ExitUsage     = 2 // "you invoked this wrong"; cobra's usage errors map here
	ExitInterrupt = 130
)

// Stdout carries results; Stderr carries refusals, warnings and chatter.
// Package variables so tests capture them; nothing else in the binary
// writes to os.Stdout/os.Stderr directly.
var (
	Stdout io.Writer = os.Stdout
	Stderr io.Writer = os.Stderr
)

// Failure is a refusal: what is wrong, whoever said so quoted verbatim, the
// remedy, and the exit code. It is an error so it can travel up through
// cobra; Report is the only thing that prints it.
type Failure struct {
	Summary string
	Detail  string
	Remedy  string
	Code    int
}

func (f *Failure) Error() string { return f.Summary }

// Option decorates a Failure.
type Option func(*Failure)

// WithDetail quotes somebody else's words - git's, the server's - under the
// summary.
func WithDetail(detail string) Option { return func(f *Failure) { f.Detail = detail } }

// WithRemedy names the fix.
func WithRemedy(remedy string) Option { return func(f *Failure) { f.Remedy = remedy } }

// WithExit sets the exit code. Zero is refused with a panic: a failure that
// exits 0 is the worst outcome this repo has, because every wrapper above
// it believes the success (cli.py:126-131).
func WithExit(code int) Option {
	if code == 0 {
		panic("ui.Fail cannot exit 0")
	}
	return func(f *Failure) { f.Code = code }
}

// Fail builds a refusal. Exit code 1 unless WithExit says otherwise.
func Fail(summary string, opts ...Option) *Failure {
	f := &Failure{Summary: summary, Code: ExitFailure}
	for _, opt := range opts {
		opt(f)
	}
	return f
}

// Usage is a refusal that is really a usage error (exit 2), indistinguishable
// from cobra's own.
func Usage(summary string, opts ...Option) *Failure {
	return Fail(summary, append(opts, WithExit(ExitUsage))...)
}

// Interrupted is the Ctrl-C outcome.
func Interrupted() *Failure {
	return Fail("interrupted", WithExit(ExitInterrupt))
}

// Report prints a failure to Stderr and returns the exit code the process
// should end with. nil is 0; a *Failure is itself; any other error is
// printed as an authored summary and exits 1 - never 0, never a panic.
func Report(err error) int {
	if err == nil {
		return ExitOK
	}
	f, ok := err.(*Failure)
	if !ok {
		f = Fail(err.Error())
	}
	if f.Code == 0 { // a Failure literal that forgot its code
		f.Code = ExitFailure
	}
	fmt.Fprintln(Stderr, style(bold+red, "Error: "+f.Summary))
	Verbatim(f.Detail)
	if f.Remedy != "" {
		Remedy(f.Remedy)
	}
	return f.Code
}

// Warn prints a fact the operator needs that is not, on its own, a failure.
// detail, if any, is quoted verbatim underneath.
func Warn(summary string, detail string) {
	fmt.Fprintln(Stderr, style(yellow, "Warning: "+summary))
	Verbatim(detail)
}

// Verbatim echoes somebody else's words EXACTLY, on stderr, indented so it
// reads as a quotation. CRLF is normalised so a Windows git does not
// double-space the quote; a blank detail prints nothing at all rather than
// an empty quotation (cli.py:97-107).
func Verbatim(text string) {
	body := strings.TrimRight(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	if strings.TrimSpace(body) == "" {
		return
	}
	for _, line := range strings.Split(body, "\n") {
		fmt.Fprintln(Stderr, style(dim, "  "+line))
	}
}

// Remedy prints the fix. Blank line first: the remedy is the part to act on.
func Remedy(text string) {
	fmt.Fprintln(Stderr)
	for _, line := range strings.Split(strings.TrimRight(text, "\n"), "\n") {
		fmt.Fprintln(Stderr, line)
	}
}

// Out writes a result line to stdout.
func Out(format string, args ...any) {
	fmt.Fprintf(Stdout, format+"\n", args...)
}

// Note writes client-side chatter to stderr (the `[dim]` lines of the Python
// CLI), so it never lands in a redirect of the results.
func Note(format string, args ...any) {
	fmt.Fprintln(Stderr, style(dim, fmt.Sprintf(format, args...)))
}
