package terminal

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/debugproto"
)

// Socket is one debug terminal connection. The websocket adapter in dial.go
// is the real one; ScriptedSocket is the test one. Recv returns the peer's
// close as an error that CloseDetails can read.
type Socket interface {
	Send(ctx context.Context, text string) error
	Recv(ctx context.Context) (string, error)
	Close() error
}

// ConsoleIO is the local keyboard and screen.
//
// NextInput returns the next chunk of keystrokes, io.EOF when local input
// ends, or ctx's error once the attach is over. WriteOutput is byte-exact.
// Size is the window, or ok=false when there is none (a pipe has no window:
// sending cols=0 would be a frame the server refuses; inventing 80x24 would
// be a lie about the terminal). SizeChanges delivers every later change of
// Size - POSIX by SIGWINCH, Windows by a 500 ms poll, a pipe never - and is
// the one seam (§12) the two console implementations differ behind.
type ConsoleIO interface {
	NextInput(ctx context.Context) ([]byte, error)
	WriteOutput(data []byte) error
	Size() (cols, rows int, ok bool)
	SizeChanges(ctx context.Context) <-chan [2]int
	// Mode names the input backend the CLI got (R1: a line-buffered
	// fallback that pretended to be a raw TTY would break every curses
	// program in a way the operator could not see): "raw-posix",
	// "raw-windows", "raw-windows (no ANSI output)" or "line-buffered".
	Mode() string
}

// StdinChunkBytes is the most raw bytes one stdin frame carries, DERIVED
// from the contract's MaxFrameBytes (§3.5): the envelope
// `{"v":1,"type":"stdin","data":""}` is stdinEnvelopeBytes long and base64
// grows 3 raw bytes into 4, so this many raw bytes fill a frame to exactly
// the bound and never past it. The Python client read 4096 at a time
// (debug_cmd.py:243); a bigger read means a pasted script goes out as
// fewer frames.
const (
	stdinEnvelopeBytes = len(`{"v":1,"type":"stdin","data":""}`)
	StdinChunkBytes    = (debugproto.MaxFrameBytes - stdinEnvelopeBytes) / 4 * 3
)

// Result is what an attach ended as. Held as a value so the command, and a
// test, read the same thing rather than parsing printed output.
type Result struct {
	ExitCode int
	Reason   string
	// Commands is every @-verb sent, in order.
	Commands []string
}

// ExitInterrupt is the exit code of an attach the caller's context ended
// (ui.ExitInterrupt; restated here so this package does not depend on ui).
const ExitInterrupt = 130

// DetachReason is the sentence a local detach ends with.
const DetachReason = "detached - the session is still paused; `lazyaf debug resume` when you are done"

// RunTerminal bridges a local console and one debug terminal socket.
//
// Returns when the server closes, the shell exits, the user detaches, or
// local input reaches EOF. A protocol-level refusal is an outcome carried in
// Result, never an error: a stated refusal is not a crash. The error return
// is for the caller's own context being cancelled before anything ended.
// Port of debug_cmd.py run_terminal.
func RunTerminal(ctx context.Context, sock Socket, console ConsoleIO, notice func(string)) (Result, error) {
	if notice == nil {
		notice = func(string) {}
	}
	parent := ctx
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	if cols, rows, ok := console.Size(); ok {
		if err := sendResize(ctx, sock, cols, rows); err != nil {
			return Result{}, err
		}
	}

	var (
		mu       sync.Mutex
		commands []string
		result   Result
		finished = make(chan struct{})
		once     sync.Once
	)
	finish := func(r Result) {
		once.Do(func() {
			mu.Lock()
			r.Commands = append([]string(nil), commands...)
			mu.Unlock()
			result = r
			close(finished)
			cancel()
		})
	}

	var wg sync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); inbound(ctx, sock, console, notice, finish) }()
	go func() { defer wg.Done(); outbound(ctx, sock, console, notice, finish, &mu, &commands) }()
	go func() {
		defer wg.Done()
		for size := range console.SizeChanges(ctx) {
			if err := sendResize(ctx, sock, size[0], size[1]); err != nil {
				return
			}
		}
	}()

	select {
	case <-finished:
	case <-parent.Done():
		// The caller's context ended (Ctrl-C at the command layer) before
		// the terminal did: not an outcome the protocol produced, so it is
		// the error return, and the exit code is the interrupt's (§5).
		cancel()
		wg.Wait()
		return Result{ExitCode: ExitInterrupt, Reason: "interrupted"}, parent.Err()
	}
	// Every goroutine has left before the Result is handed back, so a frame
	// received just before the end has reached the screen (the interleaving
	// asyncio gave the Python driver for free, made explicit).
	wg.Wait()
	return result, nil
}

func sendResize(ctx context.Context, sock Socket, cols, rows int) error {
	frame, err := debugproto.Encode(debugproto.TypeResize,
		debugproto.F("cols", cols), debugproto.F("rows", rows))
	if err != nil {
		return err
	}
	return sock.Send(ctx, frame)
}

func inbound(ctx context.Context, sock Socket, console ConsoleIO, notice func(string), finish func(Result)) {
	for {
		raw, err := sock.Recv(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return // the attach ended elsewhere; this is our own cancel
			}
			if errors.Is(err, io.EOF) {
				finish(Result{ExitCode: 1, Reason: "the server closed the terminal"})
				return
			}
			code, reason, ok := CloseDetails(err)
			if !ok {
				finish(Result{ExitCode: 1, Reason: fmt.Sprintf("terminal connection failed: %v", err)})
				return
			}
			exit := 1
			if code == debugproto.CloseNormal {
				exit = 0
			}
			finish(Result{ExitCode: exit, Reason: debugproto.CloseReason(code, reason)})
			return
		}
		frame, err := debugproto.Decode(raw)
		if err != nil {
			// R1: a frame we cannot understand is REPORTED, never dropped.
			notice(fmt.Sprintf("[protocol] %v", err))
			continue
		}
		switch frame.Type {
		case debugproto.TypeStdout:
			data, err := debugproto.DecodeBytes(frame.String("data"))
			if err != nil { // unreachable: Decode already validated it
				notice(fmt.Sprintf("[protocol] %v", err))
				continue
			}
			if err := console.WriteOutput(data); err != nil {
				finish(Result{ExitCode: 1, Reason: fmt.Sprintf("local console write failed: %v", err)})
				return
			}
		case debugproto.TypeReady:
			id := frame.String("container_id")
			if len(id) > 12 {
				id = id[:12]
			}
			notice(fmt.Sprintf("[attached] sidecar %s - %s", id, debugproto.EscapeHelp))
		case debugproto.TypeNotice:
			notice("[lazyaf] " + frame.String("text"))
		case debugproto.TypeClosed:
			reason := frame.String("reason")
			if reason == "" {
				reason = "terminal closed"
			}
			finish(Result{ExitCode: 0, Reason: reason})
			return
		}
	}
}

func outbound(ctx context.Context, sock Socket, console ConsoleIO, notice func(string), finish func(Result), mu *sync.Mutex, commands *[]string) {
	var decoder debugproto.EscapeDecoder
	for {
		chunk, err := console.NextInput(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if errors.Is(err, io.EOF) {
				finish(Result{ExitCode: 0, Reason: "local input reached EOF"})
				return
			}
			finish(Result{ExitCode: 1, Reason: fmt.Sprintf("local input failed: %v", err)})
			return
		}
		for _, action := range decoder.Feed(chunk) {
			switch action.Kind {
			case debugproto.ActionStdin:
				for _, piece := range chunks(action.Data, StdinChunkBytes) {
					frame, err := debugproto.Encode(debugproto.TypeStdin,
						debugproto.F("data", debugproto.EncodeBytes(piece)))
					if err != nil {
						finish(Result{ExitCode: 1, Reason: fmt.Sprintf("cannot encode stdin: %v", err)})
						return
					}
					if err := sock.Send(ctx, frame); err != nil {
						sendFailed(ctx, err, finish)
						return
					}
				}
			case debugproto.ActionCommand:
				mu.Lock()
				*commands = append(*commands, action.Command)
				mu.Unlock()
				frame, err := debugproto.Encode(debugproto.TypeCommand, debugproto.F("command", action.Command))
				if err != nil {
					finish(Result{ExitCode: 1, Reason: fmt.Sprintf("cannot encode command: %v", err)})
					return
				}
				if err := sock.Send(ctx, frame); err != nil {
					sendFailed(ctx, err, finish)
					return
				}
			case debugproto.ActionDetach:
				finish(Result{ExitCode: 0, Reason: DetachReason})
				return
			case debugproto.ActionUnknown:
				notice(fmt.Sprintf("[lazyaf] unknown escape key %q. %s", action.Key, debugproto.EscapeHelp))
			}
		}
	}
}

func sendFailed(ctx context.Context, err error, finish func(Result)) {
	if ctx.Err() != nil {
		return
	}
	if code, reason, ok := CloseDetails(err); ok {
		exit := 1
		if code == debugproto.CloseNormal {
			exit = 0
		}
		finish(Result{ExitCode: exit, Reason: debugproto.CloseReason(code, reason)})
		return
	}
	finish(Result{ExitCode: 1, Reason: fmt.Sprintf("terminal connection failed: %v", err)})
}

// chunks splits data into pieces of at most n bytes. A console read is
// already capped at StdinChunkBytes; this guards the line-mode path, where
// a pasted line can be any length.
func chunks(data []byte, n int) [][]byte {
	if len(data) <= n {
		return [][]byte{data}
	}
	var out [][]byte
	for len(data) > n {
		out = append(out, data[:n])
		data = data[n:]
	}
	return append(out, data)
}
