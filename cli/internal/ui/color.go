package ui

import (
	"os"

	"golang.org/x/term"
)

// The 15-line ANSI helper (§2.3). Colour wraps a string; it never parses
// one, which is the whole difference from the markup library it replaces.
// Active only when Stderr is a terminal and NO_COLOR is unset
// (https://no-color.org), checked on every call so a test that swaps Stderr
// for a buffer gets plain bytes.
const (
	bold   = "\x1b[1m"
	dim    = "\x1b[2m"
	red    = "\x1b[31m"
	yellow = "\x1b[33m"
	reset  = "\x1b[0m"
)

func colorEnabled() bool {
	if _, set := os.LookupEnv("NO_COLOR"); set {
		return false
	}
	f, ok := Stderr.(*os.File)
	return ok && term.IsTerminal(int(f.Fd()))
}

func style(code, text string) string {
	if !colorEnabled() {
		return text
	}
	return code + text + reset
}
