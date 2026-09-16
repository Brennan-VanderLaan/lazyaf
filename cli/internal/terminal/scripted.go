package terminal

import (
	"bytes"
	"context"
	"io"
	"sync"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/debugproto"
)

// Test fakes for the two seams, exported so cmd's attach tests (L3) drive
// the real driver the same way this package's tests do. They are plain
// values with no test-only dependencies; the linker drops them from a
// binary that never references them.

// ScriptedSocket is a socket that plays a script of inbound frames and
// VALIDATES every outbound frame with debugproto.Decode - the codec the
// corpus pins - so a client frame the real server would refuse fails the
// test here rather than in production (the Python fake validated with the
// SERVER codec, test_cli_debug.py:123-135; §11 states why the chain holds).
// When the script runs dry, Recv parks until ctx ends, exactly like a real
// idle terminal.
type ScriptedSocket struct {
	mu      sync.Mutex
	script  []any // string (a frame) or error (a dropped connection)
	sent    []debugproto.Frame
	invalid []error
	closed  bool
	drained chan struct{}
}

// NewScriptedSocket takes the frames (strings) and errors to play, in order.
func NewScriptedSocket(script ...any) *ScriptedSocket {
	s := &ScriptedSocket{script: append([]any(nil), script...), drained: make(chan struct{})}
	if len(s.script) == 0 {
		close(s.drained)
	}
	return s
}

// Drained is closed once the last scripted item has been handed to Recv.
// A ScriptedIO can wait on it so "local EOF" comes after the script, the
// interleaving asyncio gave the Python tests implicitly.
func (s *ScriptedSocket) Drained() <-chan struct{} { return s.drained }

func (s *ScriptedSocket) Send(ctx context.Context, text string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	frame, err := debugproto.Decode(text)
	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil {
		s.invalid = append(s.invalid, err)
		return err
	}
	s.sent = append(s.sent, frame)
	return nil
}

func (s *ScriptedSocket) Recv(ctx context.Context) (string, error) {
	s.mu.Lock()
	if len(s.script) > 0 {
		item := s.script[0]
		s.script = s.script[1:]
		if len(s.script) == 0 {
			close(s.drained)
		}
		s.mu.Unlock()
		switch v := item.(type) {
		case error:
			return "", v
		case string:
			return v, nil
		default:
			panic("ScriptedSocket script items are strings or errors")
		}
	}
	s.mu.Unlock()
	<-ctx.Done()
	return "", ctx.Err()
}

func (s *ScriptedSocket) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closed = true
	return nil
}

// Closed reports whether Close was called.
func (s *ScriptedSocket) Closed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

// Sent is every valid outbound frame, in order.
func (s *ScriptedSocket) Sent() []debugproto.Frame {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]debugproto.Frame(nil), s.sent...)
}

// Invalid is every outbound frame the codec refused. A test asserts it is
// empty.
func (s *ScriptedSocket) Invalid() []error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]error(nil), s.invalid...)
}

// FramesOf is the sent frames of one type.
func (s *ScriptedSocket) FramesOf(frameType string) []debugproto.Frame {
	var out []debugproto.Frame
	for _, f := range s.Sent() {
		if f.Type == frameType {
			out = append(out, f)
		}
	}
	return out
}

// ScriptedIO is a console whose keystrokes are a list and whose screen is a
// buffer.
type ScriptedIO struct {
	mu     sync.Mutex
	chunks [][]byte
	output bytes.Buffer
	size   *[2]int
	// ParkAtEnd: after the chunks, wait for ctx instead of reporting EOF
	// (the SERVER ends the attach, not local input).
	ParkAtEnd bool
	// WaitFor, when set, is awaited before EOF is reported (see
	// ScriptedSocket.Drained).
	WaitFor  <-chan struct{}
	ModeName string
	resizes  chan [2]int
}

// NewScriptedIO takes the keystroke chunks and the window size; a nil size
// is "no window" (a pipe).
func NewScriptedIO(chunks [][]byte, size *[2]int) *ScriptedIO {
	return &ScriptedIO{
		chunks:   append([][]byte(nil), chunks...),
		size:     size,
		ModeName: ModeLineBuffered,
		resizes:  make(chan [2]int, 8),
	}
}

// Size120x40 is the default window of the Python tests.
func Size120x40() *[2]int { return &[2]int{120, 40} }

func (s *ScriptedIO) NextInput(ctx context.Context) ([]byte, error) {
	s.mu.Lock()
	if len(s.chunks) > 0 {
		chunk := s.chunks[0]
		s.chunks = s.chunks[1:]
		s.mu.Unlock()
		return chunk, nil
	}
	s.mu.Unlock()
	if s.WaitFor != nil {
		select {
		case <-s.WaitFor:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if s.ParkAtEnd {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return nil, io.EOF
}

func (s *ScriptedIO) WriteOutput(data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.output.Write(data)
	return err
}

// Output is everything written to the screen so far.
func (s *ScriptedIO) Output() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]byte(nil), s.output.Bytes()...)
}

func (s *ScriptedIO) Size() (int, int, bool) {
	if s.size == nil {
		return 0, 0, false
	}
	return s.size[0], s.size[1], true
}

// Resize queues a window change for SizeChanges.
func (s *ScriptedIO) Resize(cols, rows int) {
	s.mu.Lock()
	s.size = &[2]int{cols, rows}
	s.mu.Unlock()
	s.resizes <- [2]int{cols, rows}
}

func (s *ScriptedIO) SizeChanges(ctx context.Context) <-chan [2]int {
	out := make(chan [2]int)
	go func() {
		defer close(out)
		for {
			select {
			case <-ctx.Done():
				return
			case size := <-s.resizes:
				select {
				case out <- size:
				case <-ctx.Done():
					return
				}
			}
		}
	}()
	return out
}

func (s *ScriptedIO) Mode() string { return s.ModeName }
