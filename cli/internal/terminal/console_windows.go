//go:build windows

package terminal

import (
	"io"
	"os"

	"golang.org/x/sys/windows"
	"golang.org/x/term"
)

// enterRaw puts the Windows console into raw mode.
//
// Input: x/term.MakeRaw clears ENABLE_LINE_INPUT / ECHO_INPUT /
// PROCESSED_INPUT and sets ENABLE_VIRTUAL_TERMINAL_INPUT, so arrow, Home/End,
// Delete and F-keys arrive as ESC sequences straight from conhost (Windows 10
// 1809+) / Windows Terminal; the msvcrt translation table and its \r -> \n
// rewrite (debug_cmd.py:313-360) are deleted, not ported (§12). With
// PROCESSED_INPUT off, Ctrl-C is 0x03 and is forwarded to the sidecar shell.
//
// Output: ENABLE_VIRTUAL_TERMINAL_PROCESSING on stdout, so the shell's colour
// codes render instead of printing as `[0m`. Best effort AND stated: if the
// call fails the mode says "raw-windows (no ANSI output)", exactly as
// debug_cmd.py:297 did.
//
// Resize: no SIGWINCH here, so a SizePollInterval poll of term.GetSize sends
// a resize frame on change (§12; an improvement over the Python client's
// initial-size-only, stated).
//
// UNVERIFIED (§12, §13.3 #6): whether os.Stdin.Read - ReadConsole under the
// hood, UTF-16 -> UTF-8 - returns per keystroke with line input disabled on
// both Windows Terminal and legacy conhost. This session ran non-
// interactively and could not exercise a real console. rawInput is the
// seam: if the owner's manual gate finds ReadConsole waits for a line, a
// ReadConsoleInput-based io.Reader (x/sys/windows, ~80 lines) slots in
// there and nothing above it changes. If neither delivers arrow keys,
// NewConsole's line-mode fallback with NoRawMode is the stated outcome.
func enterRaw(in, out *os.File) (*console, func(), error) {
	state, err := term.MakeRaw(int(in.Fd()))
	if err != nil {
		return nil, nil, err
	}
	restoreIn := func() { _ = term.Restore(int(in.Fd()), state) }

	mode := ModeRawWindows
	restoreOut := func() {}
	outHandle := windows.Handle(out.Fd())
	var outMode uint32
	if err := windows.GetConsoleMode(outHandle, &outMode); err != nil {
		mode = ModeRawWindowsNoVT
	} else if err := windows.SetConsoleMode(outHandle, outMode|windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING); err != nil {
		mode = ModeRawWindowsNoVT
	} else {
		restoreOut = func() { _ = windows.SetConsoleMode(outHandle, outMode) }
	}

	restore := func() {
		restoreOut()
		restoreIn()
	}
	return newRawConsole(rawInput(in), out, mode, termSize(out, in), pollTrigger(SizePollInterval)), restore, nil
}

// rawInput is the Windows keystroke reader seam described above. Today it
// is the console handle itself (Go's os.File uses ReadConsole with UTF-16
// -> UTF-8 translation, so non-ASCII keystrokes survive).
func rawInput(in *os.File) io.Reader {
	return in
}
