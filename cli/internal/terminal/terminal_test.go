package terminal

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/debugproto"
)

// Frame builders: debugproto.Encode is generic and the corpus proves it
// reproduces the server's bytes for every server->client type, so these
// are the server's frames in every way that matters.
func mustEncode(t *testing.T, typ string, fields ...debugproto.Field) string {
	t.Helper()
	s, err := debugproto.Encode(typ, fields...)
	if err != nil {
		t.Fatalf("Encode(%s): %v", typ, err)
	}
	return s
}

func ready(t *testing.T, containerID string) string {
	return mustEncode(t, debugproto.TypeReady,
		debugproto.F("mode", debugproto.ConnectionModeSidecar), debugproto.F("container_id", containerID))
}

func stdoutFrame(t *testing.T, payload []byte) string {
	return mustEncode(t, debugproto.TypeStdout, debugproto.F("data", debugproto.EncodeBytes(payload)))
}

func noticeFrame(t *testing.T, text string) string {
	return mustEncode(t, debugproto.TypeNotice, debugproto.F("text", text))
}

func closedFrame(t *testing.T, reason string) string {
	return mustEncode(t, debugproto.TypeClosed, debugproto.F("reason", reason))
}

func run(t *testing.T, sock *ScriptedSocket, console *ScriptedIO) (Result, []string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var notices []string
	result, err := RunTerminal(ctx, sock, console, func(s string) { notices = append(notices, s) })
	if err != nil {
		t.Fatalf("RunTerminal: %v", err)
	}
	if invalid := sock.Invalid(); len(invalid) > 0 {
		t.Fatalf("the client sent frames the codec refuses: %v", invalid)
	}
	return result, notices
}

func anyContains(items []string, want string) bool {
	for _, s := range items {
		if strings.Contains(s, want) {
			return true
		}
	}
	return false
}

// tdd/unit/scripts/test_cli_debug.py::TestTerminalUrl
func TestTerminalURL(t *testing.T) {
	t.Run("http becomes ws", func(t *testing.T) {
		got, err := TerminalURL("http://localhost:8000", "abc", "")
		if err != nil || got != "ws://localhost:8000/api/debug/abc/terminal?mode=sidecar" {
			t.Fatalf("got %q, %v", got, err)
		}
	})
	t.Run("https becomes wss", func(t *testing.T) {
		got, err := TerminalURL("https://lazyaf.example", "abc", "")
		if err != nil || !strings.HasPrefix(got, "wss://lazyaf.example/") {
			t.Fatalf("got %q, %v", got, err)
		}
	})
	t.Run("trailing slash does not double", func(t *testing.T) {
		got, err := TerminalURL("http://h:8000/", "abc", "")
		if err != nil || strings.Contains(got, "//api/debug") {
			t.Fatalf("got %q, %v", got, err)
		}
	})
	t.Run("an explicit ws url is accepted", func(t *testing.T) {
		got, err := TerminalURL("ws://h:8000", "abc", "")
		if err != nil || !strings.HasPrefix(got, "ws://h:8000/") {
			t.Fatalf("got %q, %v", got, err)
		}
	})
	t.Run("a schemeless server url is refused not guessed", func(t *testing.T) {
		// R1: guessing a scheme is how a terminal credential ends up on the
		// wire unencrypted.
		_, err := TerminalURL("localhost:8000", "abc", "")
		if err == nil || !strings.Contains(err.Error(), "scheme") {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("the token never enters the url", func(t *testing.T) {
		got, _ := TerminalURL("http://localhost:8000", "abc", "")
		if strings.Contains(got, "token") {
			t.Fatalf("the join credential belongs in the Authorization header - a query string lands in proxy logs and shell history: %q", got)
		}
	})
}

// tdd/unit/scripts/test_cli_debug.py::TestRunTerminal
func TestRunTerminal(t *testing.T) {
	t.Run("a full attach round trip", func(t *testing.T) {
		sock := NewScriptedSocket(ready(t, "c0ffee123456"), noticeFrame(t, "/workspace is rw"), stdoutFrame(t, []byte("hello\n")))
		console := NewScriptedIO([][]byte{[]byte("ls -la\n"), []byte("\x1dr")}, Size120x40())
		console.WaitFor = sock.Drained()

		result, notices := run(t, sock, console)

		if got := console.Output(); string(got) != "hello\n" { // byte-exact to the screen
			t.Fatalf("screen = %q", got)
		}
		var stdin []string
		for _, f := range sock.FramesOf(debugproto.TypeStdin) {
			b, _ := debugproto.DecodeBytes(f.String("data"))
			stdin = append(stdin, string(b))
		}
		if strings.Join(stdin, "|") != "ls -la\n" {
			t.Fatalf("stdin frames = %q", stdin)
		}
		var commands []string
		for _, f := range sock.FramesOf(debugproto.TypeCommand) {
			commands = append(commands, f.String("command"))
		}
		if strings.Join(commands, ",") != "@resume" || strings.Join(result.Commands, ",") != "@resume" {
			t.Fatalf("commands on the wire %v, in the result %v", commands, result.Commands)
		}
		// The banner and the ready line are client chatter, not shell output.
		if !anyContains(notices, "/workspace is rw") || !anyContains(notices, "c0ffee123456") {
			t.Fatalf("notices = %q", notices)
		}
	})
	t.Run("the initial window size is announced", func(t *testing.T) {
		sock := NewScriptedSocket()
		console := NewScriptedIO(nil, &[2]int{200, 50})
		run(t, sock, console)
		resize := sock.FramesOf(debugproto.TypeResize)
		if len(resize) != 1 {
			t.Fatalf("%d resize frames, want 1", len(resize))
		}
		cols, _ := resize[0].Int("cols")
		rows, _ := resize[0].Int("rows")
		if cols != 200 || rows != 50 {
			t.Fatalf("resize = %dx%d", cols, rows)
		}
	})
	t.Run("no resize is sent when the size is unknown", func(t *testing.T) {
		// A pipe has no window. Sending cols=0 would be a frame the server
		// refuses; inventing 80x24 would be a lie about the terminal.
		sock := NewScriptedSocket()
		run(t, sock, NewScriptedIO(nil, nil))
		if got := sock.FramesOf(debugproto.TypeResize); len(got) != 0 {
			t.Fatalf("resize frames = %v", got)
		}
	})
	t.Run("arbitrary output bytes survive", func(t *testing.T) {
		payload := make([]byte, 256)
		for i := range payload {
			payload[i] = byte(i)
		}
		sock := NewScriptedSocket(stdoutFrame(t, payload))
		console := NewScriptedIO(nil, Size120x40())
		console.WaitFor = sock.Drained()
		run(t, sock, console)
		if !bytes.Equal(console.Output(), payload) {
			t.Fatalf("screen = %x", console.Output())
		}
	})
	t.Run("a closed frame ends the attach with the servers reason", func(t *testing.T) {
		sock := NewScriptedSocket(ready(t, "c0ffee123456"), closedFrame(t, "resumed"))
		console := NewScriptedIO(nil, &[2]int{80, 24})
		console.ParkAtEnd = true
		result, _ := run(t, sock, console)
		if result.ExitCode != 0 || result.Reason != "resumed" {
			t.Fatalf("result = %+v", result)
		}
	})
	t.Run("local eof ends the attach", func(t *testing.T) {
		sock := NewScriptedSocket(ready(t, "c0ffee123456"))
		console := NewScriptedIO([][]byte{[]byte("whoami\n")}, Size120x40())
		result, _ := run(t, sock, console)
		if result.ExitCode != 0 || !strings.Contains(result.Reason, "EOF") {
			t.Fatalf("result = %+v", result)
		}
	})
	t.Run("detach leaves the session paused and says so", func(t *testing.T) {
		sock := NewScriptedSocket(ready(t, "c0ffee123456"))
		console := NewScriptedIO([][]byte{[]byte("\x1dd")}, Size120x40())
		console.ParkAtEnd = true
		result, _ := run(t, sock, console)
		if result.ExitCode != 0 || !strings.Contains(result.Reason, "still paused") || !strings.Contains(result.Reason, "lazyaf debug resume") {
			t.Fatalf("result = %+v", result)
		}
		// A detach must NOT resume: no command frame reached the wire.
		if got := sock.FramesOf(debugproto.TypeCommand); len(got) != 0 {
			t.Fatalf("command frames = %v", got)
		}
	})
	t.Run("an undecodable server frame is reported not dropped", func(t *testing.T) {
		// R1: a frame we cannot understand is surfaced, and the terminal
		// keeps running - a silently swallowed frame is invisible corruption.
		sock := NewScriptedSocket(`{"v":99,"type":"stdout","data":""}`, stdoutFrame(t, []byte("still here")), closedFrame(t, "the sidecar shell exited"))
		console := NewScriptedIO(nil, Size120x40())
		console.ParkAtEnd = true // the SERVER ends this one, not local EOF
		_, notices := run(t, sock, console)
		found := false
		for _, n := range notices {
			if strings.Contains(n, "[protocol]") && strings.Contains(n, "99") {
				found = true
			}
		}
		if !found {
			t.Fatalf("notices = %q", notices)
		}
		if string(console.Output()) != "still here" {
			t.Fatalf("screen = %q", console.Output())
		}
	})
	t.Run("an unknown escape key is reported and sends nothing", func(t *testing.T) {
		sock := NewScriptedSocket(ready(t, "c0ffee123456"))
		console := NewScriptedIO([][]byte{[]byte("\x1dz")}, Size120x40())
		_, notices := run(t, sock, console)
		if len(sock.FramesOf(debugproto.TypeStdin)) != 0 || len(sock.FramesOf(debugproto.TypeCommand)) != 0 {
			t.Fatalf("frames sent: %v", sock.Sent())
		}
		if !anyContains(notices, "unknown escape key") {
			t.Fatalf("notices = %q", notices)
		}
	})
	t.Run("a dropped connection is reported with its close reason", func(t *testing.T) {
		dropped := websocket.CloseError{Code: debugproto.CloseBoundExceeded, Reason: "frame exceeds 65536 bytes"}
		sock := NewScriptedSocket(dropped)
		console := NewScriptedIO(nil, Size120x40())
		console.ParkAtEnd = true
		result, _ := run(t, sock, console)
		if result.ExitCode != 1 || !strings.Contains(result.Reason, "frame exceeds 65536 bytes") || !strings.Contains(result.Reason, strconv.Itoa(debugproto.CloseBoundExceeded)) {
			t.Fatalf("result = %+v", result)
		}
	})
	t.Run("a normal close from the server is exit 0", func(t *testing.T) {
		sock := NewScriptedSocket(websocket.CloseError{Code: websocket.StatusNormalClosure, Reason: ""})
		console := NewScriptedIO(nil, Size120x40())
		console.ParkAtEnd = true
		result, _ := run(t, sock, console)
		if result.ExitCode != 0 || !strings.Contains(result.Reason, "1000") {
			t.Fatalf("result = %+v", result)
		}
	})
	t.Run("a resize after attach sends a resize frame", func(t *testing.T) {
		// §12: POSIX by SIGWINCH, Windows by poll, both through SizeChanges.
		sock := NewScriptedSocket(ready(t, "c0ffee123456"))
		console := NewScriptedIO(nil, &[2]int{80, 24})
		console.ParkAtEnd = true
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		done := make(chan Result, 1)
		go func() {
			r, _ := RunTerminal(ctx, sock, console, func(string) {})
			done <- r
		}()
		console.Resize(132, 43)
		deadline := time.Now().Add(5 * time.Second)
		for len(sock.FramesOf(debugproto.TypeResize)) < 2 && time.Now().Before(deadline) {
			time.Sleep(5 * time.Millisecond)
		}
		cancel()
		if r := <-done; r.ExitCode != ExitInterrupt {
			t.Fatalf("after the caller's cancel: %+v", r)
		}
		frames := sock.FramesOf(debugproto.TypeResize)
		if len(frames) != 2 {
			t.Fatalf("%d resize frames, want the initial one and the change", len(frames))
		}
		cols, _ := frames[1].Int("cols")
		rows, _ := frames[1].Int("rows")
		if cols != 132 || rows != 43 {
			t.Fatalf("second resize = %dx%d", cols, rows)
		}
	})
	t.Run("the callers context ending is the error return not a hang", func(t *testing.T) {
		sock := NewScriptedSocket(ready(t, "c0ffee123456"))
		console := NewScriptedIO(nil, nil)
		console.ParkAtEnd = true
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		defer cancel()
		result, err := RunTerminal(ctx, sock, console, nil)
		if !errors.Is(err, context.DeadlineExceeded) || result.ExitCode != ExitInterrupt {
			t.Fatalf("result %+v err %v", result, err)
		}
	})
	t.Run("a stdin chunk at the bound fits one frame and a pasted line is split", func(t *testing.T) {
		big := bytes.Repeat([]byte("x"), StdinChunkBytes*2+1)
		sock := NewScriptedSocket()
		console := NewScriptedIO([][]byte{big}, nil)
		run(t, sock, console)
		frames := sock.FramesOf(debugproto.TypeStdin)
		if len(frames) != 3 {
			t.Fatalf("%d stdin frames, want 3", len(frames))
		}
		var joined []byte
		for _, f := range frames {
			b, _ := debugproto.DecodeBytes(f.String("data"))
			joined = append(joined, b...)
		}
		if !bytes.Equal(joined, big) {
			t.Fatal("the split lost bytes")
		}
	})
}

func TestStdinChunkBoundIsDerivedFromTheContract(t *testing.T) {
	// §3.5: the client caps a stdin read so no frame it sends can exceed
	// MAX_FRAME_BYTES, and the cap is computed from the constant the corpus
	// checks. The server's check is `len > MAX_FRAME_BYTES`
	// (backend/app/routers/debug.py:510), so exactly the bound is accepted.
	at := mustEncode(t, debugproto.TypeStdin, debugproto.F("data", debugproto.EncodeBytes(bytes.Repeat([]byte{0xff}, StdinChunkBytes))))
	if len(at) > debugproto.MaxFrameBytes {
		t.Fatalf("a full chunk encodes to %d bytes, over the %d bound", len(at), debugproto.MaxFrameBytes)
	}
	over := mustEncode(t, debugproto.TypeStdin, debugproto.F("data", debugproto.EncodeBytes(bytes.Repeat([]byte{0xff}, StdinChunkBytes+3))))
	if len(over) <= debugproto.MaxFrameBytes {
		t.Fatalf("the chunk bound is slack: %d+3 raw bytes still fit in %d", StdinChunkBytes, len(over))
	}
	if StdinChunkBytes < 48*1024-64 || StdinChunkBytes > 48*1024 {
		t.Fatalf("StdinChunkBytes = %d, expected about 48 KiB", StdinChunkBytes)
	}
}

// tdd/unit/scripts/test_cli_debug.py::TestCloseDetails
func TestCloseDetails(t *testing.T) {
	t.Run("reads the close frame out of a wrapped error", func(t *testing.T) {
		err := errors.Join(errors.New("failed to read"), websocket.CloseError{Code: 4401, Reason: "missing or invalid join token"})
		code, reason, ok := CloseDetails(err)
		if !ok || code != 4401 || reason != "missing or invalid join token" {
			t.Fatalf("CloseDetails = %d %q %v", code, reason, ok)
		}
	})
	t.Run("a plain error yields nothing rather than inventing a code", func(t *testing.T) {
		if _, _, ok := CloseDetails(errors.New("boom")); ok {
			t.Fatal("a plain error was read as a close frame")
		}
	})
	t.Run("a rejected handshake reports its http status", func(t *testing.T) {
		status, ok := HandshakeStatus(&HandshakeError{Status: 403, Err: errors.New("403")})
		if !ok || status != 403 {
			t.Fatalf("HandshakeStatus = %d %v", status, ok)
		}
	})
	t.Run("a close code is not mistaken for a handshake status", func(t *testing.T) {
		if _, ok := HandshakeStatus(websocket.CloseError{Code: 4403}); ok {
			t.Fatal("a close error was read as a handshake status")
		}
		if _, ok := HandshakeStatus(errors.New("boom")); ok {
			t.Fatal("a plain error was read as a handshake status")
		}
	})
	t.Run("the upgrade refusal names where the reason can be read", func(t *testing.T) {
		// R1: the CLI never invents a reason it was not given, and never
		// leaves the operator with a bare number.
		text := UpgradeRefused(403, "sess-1")
		for _, want := range []string{"403", "lazyaf debug status sess-1", "expired"} {
			if !strings.Contains(text, want) {
				t.Fatalf("UpgradeRefused lacks %q:\n%s", want, text)
			}
		}
	})
	t.Run("every close code has a sentence when the reason is dropped", func(t *testing.T) {
		// An intermediary that strips the reason must not turn a stated
		// refusal into a bare number.
		for code := range debugproto.CloseCodeMeanings {
			text := debugproto.CloseReason(code, "")
			if !strings.Contains(text, strconv.Itoa(code)) || len(text) <= len(strconv.Itoa(code))+8 {
				t.Fatalf("CloseReason(%d, \"\") = %q", code, text)
			}
		}
		if got := debugproto.CloseReason(4999, "  "); !strings.Contains(got, "4999") {
			t.Fatalf("an unlisted code is still named: %q", got)
		}
		if got := debugproto.CloseReason(debugproto.NoCloseCode, "just words"); got != "just words" {
			t.Fatalf("a reason with no code is printed bare: %q", got)
		}
	})
	t.Run("the servers reason wins when it survives", func(t *testing.T) {
		if got := debugproto.CloseReason(4403, "remote steps do not attach"); !strings.HasPrefix(got, "remote steps do not attach") {
			t.Fatalf("CloseReason = %q", got)
		}
	})
	t.Run("every close code the server can send has a meaning", func(t *testing.T) {
		// Read from the corpus, so a CLOSE_* the server gains lands here.
		abs, _ := filepath.Abs("../../../tdd/contracts/debug_terminal.v1.json")
		raw, err := os.ReadFile(abs)
		if err != nil {
			t.Fatalf("the codec corpus is missing at %s (%v) - run from a LazyAF checkout", abs, err)
		}
		var corpus struct {
			Constants struct {
				Scalars map[string]json.RawMessage `json:"scalars"`
			} `json:"constants"`
		}
		if err := json.Unmarshal(raw, &corpus); err != nil {
			t.Fatal(err)
		}
		seen := 0
		for name, value := range corpus.Constants.Scalars {
			if !strings.HasPrefix(name, "CLOSE_") {
				continue
			}
			seen++
			var code int
			if err := json.Unmarshal(value, &code); err != nil {
				t.Fatalf("%s = %s is not an int", name, value)
			}
			if _, ok := debugproto.CloseCodeMeanings[code]; !ok {
				t.Errorf("the server gained close code %s=%d the CLI cannot explain", name, code)
			}
		}
		if seen == 0 {
			t.Fatal("the corpus lists no CLOSE_* codes")
		}
	})
}

func TestAttach(t *testing.T) {
	ctx := context.Background()
	console := NewScriptedIO(nil, nil)
	t.Run("a rejected handshake becomes the upgrade refusal", func(t *testing.T) {
		dial := func(context.Context, string, string) (Socket, error) {
			return nil, &HandshakeError{Status: 403, Err: errors.New("403")}
		}
		result, err := Attach(ctx, dial, "ws://h/x", "tok", console, nil, "sess-1")
		if err != nil || result.ExitCode != 1 || !strings.Contains(result.Reason, "lazyaf debug status sess-1") {
			t.Fatalf("result %+v err %v", result, err)
		}
	})
	t.Run("a close at dial becomes its sentence", func(t *testing.T) {
		dial := func(context.Context, string, string) (Socket, error) {
			return nil, websocket.CloseError{Code: 4404, Reason: ""}
		}
		result, err := Attach(ctx, dial, "ws://h/x", "tok", console, nil, "sess-1")
		if err != nil || result.ExitCode != 1 || !strings.Contains(result.Reason, "unknown debug session") {
			t.Fatalf("result %+v err %v", result, err)
		}
	})
	t.Run("an unrecognised failure is returned not dressed up", func(t *testing.T) {
		boom := errors.New("dns exploded")
		dial := func(context.Context, string, string) (Socket, error) { return nil, boom }
		if _, err := Attach(ctx, dial, "ws://h/x", "tok", console, nil, "sess-1"); !errors.Is(err, boom) {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("a dialed socket is driven and closed", func(t *testing.T) {
		sock := NewScriptedSocket(closedFrame(t, "resumed"))
		dial := func(context.Context, string, string) (Socket, error) { return sock, nil }
		c := NewScriptedIO(nil, nil)
		c.ParkAtEnd = true
		result, err := Attach(ctx, dial, "ws://h/x", "tok", c, nil, "sess-1")
		if err != nil || result.Reason != "resumed" || !sock.Closed() {
			t.Fatalf("result %+v err %v closed %v", result, err, sock.Closed())
		}
	})
}

// tdd/unit/scripts/test_cli_debug.py::TestConsoleIO
func TestConsoleIO(t *testing.T) {
	t.Run("a pipe is line buffered and says so", func(t *testing.T) {
		// R1: a line-buffered stream that claimed to be a raw TTY would break
		// every full-screen program in a way the operator cannot see.
		c := NewPipeConsole(strings.NewReader("one\ntwo\nthree"), io.Discard)
		if c.Mode() != ModeLineBuffered {
			t.Fatalf("mode = %q", c.Mode())
		}
		ctx := context.Background()
		for _, want := range []string{"one\n", "two\n", "three"} {
			got, err := c.NextInput(ctx)
			if err != nil || string(got) != want {
				t.Fatalf("NextInput = %q, %v; want %q", got, err, want)
			}
		}
		if _, err := c.NextInput(ctx); !errors.Is(err, io.EOF) {
			t.Fatalf("after the last line: %v, want io.EOF", err)
		}
		if _, _, ok := c.Size(); ok {
			t.Fatal("a pipe has no window")
		}
	})
	t.Run("next input honours the context while the read is parked", func(t *testing.T) {
		pr, pw := io.Pipe()
		defer pw.Close()
		c := NewPipeConsole(pr, io.Discard)
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		defer cancel()
		if _, err := c.NextInput(ctx); !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("NextInput = %v, want the context's error", err)
		}
	})
	t.Run("output reaches a binary stream byte exact", func(t *testing.T) {
		var out bytes.Buffer
		c := NewPipeConsole(strings.NewReader(""), &out)
		payload := make([]byte, 256)
		for i := range payload {
			payload[i] = byte(i)
		}
		if err := c.WriteOutput(payload); err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(out.Bytes(), payload) {
			t.Fatalf("screen = %x", out.Bytes())
		}
	})
	t.Run("size is a positive pair or none", func(t *testing.T) {
		// Whatever this process's stdout is (a pipe under the harness, a
		// terminal by hand), termSize never returns a zero or negative pair.
		cols, rows, ok := termSize(os.Stdout, os.Stdin)()
		if ok && (cols <= 0 || rows <= 0) {
			t.Fatalf("size = %dx%d ok", cols, rows)
		}
	})
	t.Run("a non tty stdin gets the line mode console", func(t *testing.T) {
		r, w, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		defer r.Close()
		defer w.Close()
		c, restore, warning := NewConsole(r, w)
		defer restore()
		if c.Mode() != ModeLineBuffered || warning != "" {
			t.Fatalf("mode %q warning %q", c.Mode(), warning)
		}
	})
	t.Run("the size watcher emits only on change", func(t *testing.T) {
		// The watcher's READS are the state this test waits on. A trigger
		// send returning only proves the watcher took the trigger, not that
		// it has re-read the size: change the size in that gap and the
		// "unchanged" trigger sees the new size, emits, and blocks in a send
		// nobody receives while the test blocks on its next trigger. Under
		// load that was a ten-minute hang, not a failure. Every wait below is
		// generous and named, so a loaded host fails only if the watcher is
		// wrong, and says which step it was wrong at.
		const deadline = 10 * time.Second
		var mu = make(chan [2]int, 1)
		mu <- [2]int{80, 24}
		reads := make(chan struct{}, 16)
		size := func() (int, int, bool) {
			s := <-mu
			mu <- s
			reads <- struct{}{}
			return s[0], s[1], true
		}
		setSize := func(s [2]int) {
			<-mu
			mu <- s
		}
		awaitRead := func(when string) {
			t.Helper()
			select {
			case <-reads:
			case <-time.After(deadline):
				t.Fatalf("the watcher never read the size %s", when)
			}
		}
		trigger := make(chan struct{})
		fire := func(when string) {
			t.Helper()
			select {
			case trigger <- struct{}{}:
			case <-time.After(deadline):
				t.Fatalf("the watcher took no trigger %s: it is stuck emitting an event nothing caused", when)
			}
		}
		expect := func(want [2]int, changes <-chan [2]int) {
			t.Helper()
			select {
			case got := <-changes:
				if got != want {
					t.Fatalf("change = %v, want %v", got, want)
				}
			case <-time.After(deadline):
				t.Fatalf("no resize event for %v", want)
			}
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		changes := watchSize(ctx, size, trigger)
		awaitRead("at start")

		fire("with the size unchanged")
		awaitRead("for the unchanged trigger")

		setSize([2]int{100, 30})
		fire("after the size changed")
		expect([2]int{100, 30}, changes)
		awaitRead("for the changed trigger")

		fire("with the size unchanged again")
		awaitRead("for the second unchanged trigger")

		// `changes` is unbuffered: a spurious event would leave the watcher
		// blocked in its send, deaf to triggers. It taking this trigger and
		// the NEXT event being the new size is the proof that the unchanged
		// triggers emitted nothing - no clock window asserts an absence.
		setSize([2]int{120, 40})
		fire("after the second change")
		expect([2]int{120, 40}, changes)
	})
	t.Run("the poll trigger fires and stops with the context", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		ch := pollTrigger(5 * time.Millisecond)(ctx)
		select {
		case <-ch:
		case <-time.After(2 * time.Second):
			t.Fatal("the poll never fired")
		}
		cancel()
		deadline := time.After(2 * time.Second)
		for {
			select {
			case _, ok := <-ch:
				if !ok {
					return
				}
			case <-deadline:
				t.Fatal("the poll did not stop after cancel")
			}
		}
	})
}

// drain reads until the peer closes; a hijacked request's context does not
// end on client close, so this is how a handler learns the test is over.
func drain(conn *websocket.Conn) {
	for {
		if _, _, err := conn.Read(context.Background()); err != nil {
			return
		}
	}
}

// wsServer accepts one websocket and hands it to serve.
func wsServer(t *testing.T, serve func(t *testing.T, conn *websocket.Conn, r *http.Request)) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		serve(t, conn, r)
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http")
}

func TestDialSendsTheBearerHeader(t *testing.T) {
	// debug_cmd.py:71-88 / test_cli_debug.py:231: the token travels in the
	// Authorization header and never in the URL.
	got := make(chan string, 1)
	url := wsServer(t, func(t *testing.T, conn *websocket.Conn, r *http.Request) {
		got <- r.Header.Get("Authorization") + "|" + r.URL.RawQuery
		_ = conn.Write(context.Background(), websocket.MessageText, []byte(closedFrame(t, "bye")))
		drain(conn)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	sock, err := Dial(ctx, url+"/api/debug/s/terminal?mode=sidecar", "tok-123")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer sock.Close()
	if seen := <-got; seen != "Bearer tok-123|mode=sidecar" {
		t.Fatalf("server saw %q", seen)
	}
	if frame, err := sock.Recv(ctx); err != nil || !strings.Contains(frame, "bye") {
		t.Fatalf("Recv = %q, %v", frame, err)
	}
}

func TestDialReportsARejectedHandshake(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(403)
	}))
	t.Cleanup(srv.Close)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http")+"/x", "tok")
	status, ok := HandshakeStatus(err)
	if !ok || status != 403 {
		t.Fatalf("Dial error %v -> status %d %v", err, status, ok)
	}
}

func TestReadLimitIsTheContractBound(t *testing.T) {
	// §2.4 / §15.1 #9: coder/websocket defaults to a 32 KiB read limit. A
	// frame of exactly MaxFrameBytes must arrive; one byte more must not,
	// which proves the limit is the contract's number and not merely "big".
	makeFrame := func(n int) string {
		// A valid stdout frame padded to exactly n bytes: the base64 fills most
		// of it, a harmless extra field ("pad") takes up the remainder.
		prefix, suffix := `{"v":1,"type":"stdout","data":"`, `"}`
		raw := (n - len(prefix) - len(suffix) - 12) / 4 * 3
		base := prefix + debugproto.EncodeBytes(bytes.Repeat([]byte{0}, raw)) + suffix
		extra := n - len(base) - len(`,"pad":""`)
		return strings.TrimSuffix(base, "}") + `,"pad":"` + strings.Repeat("x", extra) + `"}`
	}
	for _, tc := range []struct {
		name string
		size int
		ok   bool
	}{
		{"exactly the bound", debugproto.MaxFrameBytes, true},
		{"one over", debugproto.MaxFrameBytes + 1, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			frame := makeFrame(tc.size)
			if len(frame) != tc.size {
				t.Fatalf("test frame is %d bytes, want %d", len(frame), tc.size)
			}
			url := wsServer(t, func(t *testing.T, conn *websocket.Conn, r *http.Request) {
				_ = conn.Write(context.Background(), websocket.MessageText, []byte(frame))
				drain(conn)
			})
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			sock, err := Dial(ctx, url+"/x", "tok")
			if err != nil {
				t.Fatalf("Dial: %v", err)
			}
			defer sock.Close()
			got, err := sock.Recv(ctx)
			if tc.ok {
				if err != nil {
					t.Fatalf("a %d-byte frame was refused: %v (the read limit is below MaxFrameBytes)", tc.size, err)
				}
				if got != frame {
					t.Fatal("the frame arrived altered")
				}
				return
			}
			if err == nil {
				t.Fatalf("a %d-byte frame was accepted; the read limit is above MaxFrameBytes", tc.size)
			}
		})
	}
}
