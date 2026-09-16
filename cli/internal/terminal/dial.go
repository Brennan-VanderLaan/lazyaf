package terminal

import (
	"context"
	"errors"
	"fmt"

	"github.com/coder/websocket"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/debugproto"
)

// The websocket seam. This file is the ONLY importer of coder/websocket in
// the binary (api.TestOnlyAPIImportsNetHTTP), and it deliberately does not
// import net/http: the header goes in as a plain map and the rejected
// handshake's status is read off the response value, so the "one HTTP
// idiom" rule holds by construction.

// Dialer opens a terminal socket. Dial is the real one; a test hands
// Attach a fake.
type Dialer func(ctx context.Context, url, token string) (Socket, error)

// HandshakeError is a rejected upgrade: the socket was never accepted, so
// no close frame exists and only the HTTP status survives (debug_cmd.py:
// 119-137). Starlette turns a pre-accept close into a plain 403 during the
// handshake, so the sentence the server wrote is dropped on the floor.
type HandshakeError struct {
	Status int
	Err    error
}

func (e *HandshakeError) Error() string {
	return fmt.Sprintf("the server refused the terminal upgrade (HTTP %d): %v", e.Status, e.Err)
}

func (e *HandshakeError) Unwrap() error { return e.Err }

// Dial opens the terminal socket with the join token in an
// `Authorization: Bearer` header - never in the URL (TerminalURL's rule;
// pinned by TestTerminalURL and TestDialSendsTheBearerHeader).
//
// coder/websocket's default read limit is 32 KiB, below the contract's
// MaxFrameBytes; without SetReadLimit a legitimate 64 KiB stdout frame
// would close the socket with 1009 (§2.4, §15.1 #9).
// TestReadLimitIsTheContractBound pins it.
func Dial(ctx context.Context, url, token string) (Socket, error) {
	conn, resp, err := websocket.Dial(ctx, url, &websocket.DialOptions{
		HTTPHeader: map[string][]string{"Authorization": {"Bearer " + token}},
	})
	if err != nil {
		if resp != nil && resp.StatusCode != 0 {
			return nil, &HandshakeError{Status: resp.StatusCode, Err: err}
		}
		return nil, err
	}
	conn.SetReadLimit(debugproto.MaxFrameBytes)
	return &wsSocket{conn: conn}, nil
}

type wsSocket struct {
	conn *websocket.Conn
}

func (s *wsSocket) Send(ctx context.Context, text string) error {
	return s.conn.Write(ctx, websocket.MessageText, []byte(text))
}

func (s *wsSocket) Recv(ctx context.Context) (string, error) {
	_, data, err := s.conn.Read(ctx)
	if err != nil {
		return "", err
	}
	return string(data), nil
}

func (s *wsSocket) Close() error {
	return s.conn.Close(websocket.StatusNormalClosure, "")
}

// CloseDetails reads (code, reason) out of an error carrying the peer's
// close frame. A refusal at the upgrade travels ONLY in the close frame, so
// failing to read it here would turn every stated refusal into "connection
// closed". Port of debug_cmd.py close_details; coder/websocket has one
// CloseError shape, so the rcvd-vs-flat tolerance is retired (§11).
func CloseDetails(err error) (code int, reason string, ok bool) {
	var ce websocket.CloseError
	if errors.As(err, &ce) {
		return int(ce.Code), ce.Reason, true
	}
	return 0, "", false
}

// HandshakeStatus is the HTTP status a rejected WebSocket handshake came
// back with, if the error is one. A close code is never mistaken for it.
func HandshakeStatus(err error) (int, bool) {
	var he *HandshakeError
	if errors.As(err, &he) {
		return he.Status, true
	}
	return 0, false
}

// UpgradeRefused is what the CLI says when the server refuses the UPGRADE
// itself. The client cannot invent the reason it was not given, so it says
// exactly that and names where the reason CAN be read. Anything vaguer
// would be the CLI pretending to know why (R1).
func UpgradeRefused(status int, session string) string {
	return fmt.Sprintf(
		"the server refused the terminal upgrade (HTTP %d). A rejected WebSocket "+
			"handshake carries no reason, so read it from the session itself:\n"+
			"    lazyaf debug status %s\n"+
			"Most often the join credential expired - they are short-lived and "+
			"re-mintable, so run `lazyaf debug attach` again for a fresh one.",
		status, session)
}

// Attach opens the terminal socket through dial and hands it to
// RunTerminal.
//
// Two refusal shapes, and neither may reach the operator as a stack trace:
// a close code (the socket was accepted, then closed) and a rejected
// handshake (it never was). Both come back as a Result carrying a sentence;
// anything else is returned as an error, because an unrecognised failure
// must not be dressed up as a stated refusal. Port of debug_cmd.py
// attach_socket.
func Attach(ctx context.Context, dial Dialer, url, token string, console ConsoleIO, notice func(string), sessionID string) (Result, error) {
	sock, err := dial(ctx, url, token)
	if err != nil {
		if code, reason, ok := CloseDetails(err); ok {
			return Result{ExitCode: 1, Reason: debugproto.CloseReason(code, reason)}, nil
		}
		if status, ok := HandshakeStatus(err); ok {
			return Result{ExitCode: 1, Reason: UpgradeRefused(status, sessionID)}, nil
		}
		return Result{}, err
	}
	defer sock.Close()
	return RunTerminal(ctx, sock, console, notice)
}
