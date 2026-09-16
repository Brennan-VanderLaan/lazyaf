package terminal

import (
	"bufio"
	"context"
	"io"
	"os"
	"sync"
	"time"

	"golang.org/x/term"
)

// Local console I/O.
//
// A blocking read on stdin cannot be cancelled, so a goroutine owns it and
// pushes chunks into a channel (the Python client's daemon thread,
// debug_cmd.py:158-166); NextInput selects on that channel and the context,
// so the attach can end while the read is still parked. Three input
// backends, and the CLI SAYS which one it got (Mode; R1):
//
//   - POSIX tty: x/term raw mode, byte-exact (console_posix.go)
//   - Windows console: x/term raw mode + VT input, byte-exact (console_windows.go)
//   - not a tty (a pipe, CI): line-buffered stdin, stated on attach (here)

// Mode names.
const (
	ModeRawPosix       = "raw-posix"
	ModeRawWindows     = "raw-windows"
	ModeRawWindowsNoVT = "raw-windows (no ANSI output)"
	ModeLineBuffered   = "line-buffered"
)

// SizePollInterval is how often the Windows console checks its window size,
// there being no SIGWINCH (§12).
const SizePollInterval = 500 * time.Millisecond

// NoRawMode is the notice printed when a tty cannot enter raw mode: attach
// degrades to LINE mode and says so, never a silent degrade to garbage
// keystrokes (§12 "does not work, and says so").
const NoRawMode = "this console cannot enter raw mode; running line-buffered - " +
	"use Windows Terminal for arrow keys"

// readResult is one read off the input goroutine.
type readResult struct {
	data []byte
	err  error
}

// inputPump owns the blocking reads. split is how one read is cut: whole
// lines for a pipe, up to StdinChunkBytes for a raw console.
type inputPump struct {
	split func() ([]byte, error)
	ch    chan readResult
	once  sync.Once
}

func (p *inputPump) start() {
	p.once.Do(func() {
		p.ch = make(chan readResult)
		go func() {
			defer close(p.ch)
			for {
				data, err := p.split()
				if len(data) > 0 {
					p.ch <- readResult{data: data}
				}
				if err != nil {
					if err != io.EOF {
						p.ch <- readResult{err: err}
					}
					return
				}
			}
		}()
	})
}

// next is NextInput for both console kinds: the next chunk, io.EOF when the
// pump is done, or ctx's error.
func (p *inputPump) next(ctx context.Context) ([]byte, error) {
	p.start()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case r, ok := <-p.ch:
		if !ok {
			return nil, io.EOF
		}
		return r.data, r.err
	}
}

// console is the shared implementation; the raw backends differ only in
// how they enter/leave raw mode and how they learn of a resize.
type console struct {
	pump    inputPump
	out     io.Writer
	mode    string
	getSize func() (cols, rows int, ok bool)
	changes func(ctx context.Context) <-chan [2]int
}

func (c *console) NextInput(ctx context.Context) ([]byte, error) { return c.pump.next(ctx) }

// WriteOutput is byte-exact and unbuffered: an *os.File write reaches the
// console before this returns, so there is no flush to forget (the Python
// client flushed after every write, debug_cmd.py:208-214).
func (c *console) WriteOutput(data []byte) error {
	_, err := c.out.Write(data)
	return err
}

func (c *console) Size() (int, int, bool) {
	if c.getSize == nil {
		return 0, 0, false
	}
	return c.getSize()
}

func (c *console) SizeChanges(ctx context.Context) <-chan [2]int {
	if c.changes == nil {
		ch := make(chan [2]int)
		close(ch)
		return ch
	}
	return c.changes(ctx)
}

func (c *console) Mode() string { return c.mode }

// NewPipeConsole is the line-buffered console over any reader/writer: what
// a pipe, a harness or `--print-credential`'s tests get. It has no window,
// so Size is never ok and SizeChanges never fires.
func NewPipeConsole(in io.Reader, out io.Writer) ConsoleIO {
	r := bufio.NewReader(in)
	return &console{
		pump: inputPump{split: func() ([]byte, error) { return r.ReadBytes('\n') }},
		out:  out,
		mode: ModeLineBuffered,
	}
}

// newRawConsole is the byte-exact console over an input already in raw mode.
// Reads are capped at StdinChunkBytes so every stdin frame fits the
// contract's bound (run_terminal.go).
func newRawConsole(in io.Reader, out io.Writer, mode string, getSize func() (int, int, bool), trigger func(ctx context.Context) <-chan struct{}) *console {
	buf := make([]byte, StdinChunkBytes)
	return &console{
		pump: inputPump{split: func() ([]byte, error) {
			n, err := in.Read(buf)
			return append([]byte(nil), buf[:n]...), err
		}},
		out:     out,
		mode:    mode,
		getSize: getSize,
		changes: func(ctx context.Context) <-chan [2]int {
			return watchSize(ctx, getSize, trigger(ctx))
		},
	}
}

// watchSize re-reads the window size on every trigger and emits it only
// when it changed. The trigger is the seam (§12): SIGWINCH on POSIX, a
// SizePollInterval ticker on Windows, a hand-driven channel in the tests.
func watchSize(ctx context.Context, getSize func() (int, int, bool), trigger <-chan struct{}) <-chan [2]int {
	out := make(chan [2]int)
	go func() {
		defer close(out)
		lastCols, lastRows, known := getSize()
		for {
			select {
			case <-ctx.Done():
				return
			case _, ok := <-trigger:
				if !ok {
					return
				}
			}
			cols, rows, ok := getSize()
			if !ok || (known && cols == lastCols && rows == lastRows) {
				continue
			}
			lastCols, lastRows, known = cols, rows, true
			select {
			case out <- [2]int{cols, rows}:
			case <-ctx.Done():
				return
			}
		}
	}()
	return out
}

// pollTrigger fires every interval until ctx ends.
func pollTrigger(interval time.Duration) func(ctx context.Context) <-chan struct{} {
	return func(ctx context.Context) <-chan struct{} {
		ch := make(chan struct{})
		go func() {
			defer close(ch)
			t := time.NewTicker(interval)
			defer t.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-t.C:
					select {
					case ch <- struct{}{}:
					case <-ctx.Done():
						return
					}
				}
			}
		}()
		return ch
	}
}

// termSize reads the window from stdout, falling back to stdin (a
// redirected stdout still has a keyboard with a window behind it).
func termSize(out, in *os.File) func() (int, int, bool) {
	return func() (int, int, bool) {
		for _, f := range []*os.File{out, in} {
			if f == nil {
				continue
			}
			cols, rows, err := term.GetSize(int(f.Fd()))
			if err == nil && cols > 0 && rows > 0 {
				return cols, rows, true
			}
		}
		return 0, 0, false
	}
}

// NewConsole picks the backend for the real stdin/stdout: raw when stdin is
// a terminal that can enter raw mode, line-buffered otherwise. restore
// undoes the terminal modes and MUST be deferred by the caller (on Ctrl-C
// and panic too, §12). warning is non-empty when a tty could not enter raw
// mode and the attach is running line-buffered instead - the caller prints
// it; nothing degrades silently.
func NewConsole(in, out *os.File) (io ConsoleIO, restore func(), warning string) {
	if !term.IsTerminal(int(in.Fd())) {
		return NewPipeConsole(in, out), func() {}, ""
	}
	c, restore, err := enterRaw(in, out)
	if err != nil {
		return NewPipeConsole(in, out), func() {}, NoRawMode + " (" + err.Error() + ")"
	}
	return c, restore, ""
}
