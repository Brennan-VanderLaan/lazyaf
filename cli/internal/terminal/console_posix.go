//go:build !windows

package terminal

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"golang.org/x/term"
)

// enterRaw puts a POSIX tty into raw mode (termios: no line buffering, no
// echo, no ISIG - Ctrl-C arrives as 0x03 and is forwarded to the sidecar
// shell; Ctrl-] then d is the local exit). Resizes arrive by SIGWINCH.
func enterRaw(in, out *os.File) (*console, func(), error) {
	state, err := term.MakeRaw(int(in.Fd()))
	if err != nil {
		return nil, nil, err
	}
	restore := func() { _ = term.Restore(int(in.Fd()), state) }
	trigger := func(ctx context.Context) <-chan struct{} {
		ch := make(chan struct{})
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGWINCH)
		go func() {
			defer close(ch)
			defer signal.Stop(sig)
			for {
				select {
				case <-ctx.Done():
					return
				case <-sig:
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
	return newRawConsole(in, out, ModeRawPosix, termSize(out, in), trigger), restore, nil
}
