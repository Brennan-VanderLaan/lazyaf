// Package terminal is `lazyaf debug attach`: the raw-TTY terminal client.
//
// Port of cli/lazyaf/debug_cmd.py. The shape, and why: three layers, so two
// of them are testable without a TTY or a network. RunTerminal drives the
// protocol against a Socket and a ConsoleIO; the websocket (dial.go) and the
// raw console (console_*.go) are the outermost layer only. That is how
// TestRunTerminal drives a FULL attach - ready, notice, stdout, keystrokes,
// @resume, close - against a socket that validates every outbound frame with
// the codec the corpus pins, with no doubles inside the code under test.
//
// The codec is debugproto; nothing in this package builds a frame by hand.
package terminal

import (
	"fmt"
	"strings"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/debugproto"
)

// TerminalURL is the ws:// URL of a session's terminal socket.
//
// The join token is NOT put in the query string even though the endpoint
// accepts it there: query strings land in proxy logs and shell history. The
// client presents it in an `Authorization: Bearer` header (dial.go), which
// the endpoint prefers over the query parameter. A schemeless server URL is
// refused, not guessed - the same rule api.ResolveServerURL applies (R3),
// because guessing a scheme is how a credential ends up on the wire in the
// clear. Port of debug_cmd.py:71-88.
func TerminalURL(serverURL, sessionID, mode string) (string, error) {
	base := strings.TrimRight(serverURL, "/")
	switch {
	case strings.HasPrefix(base, "https://"):
		base = "wss://" + strings.TrimPrefix(base, "https://")
	case strings.HasPrefix(base, "http://"):
		base = "ws://" + strings.TrimPrefix(base, "http://")
	case strings.HasPrefix(base, "ws://"), strings.HasPrefix(base, "wss://"):
	default:
		return "", fmt.Errorf("server URL %q has no http(s):// or ws(s):// scheme", serverURL)
	}
	if mode == "" {
		mode = debugproto.ConnectionModeSidecar
	}
	return fmt.Sprintf("%s/api/debug/%s/terminal?mode=%s", base, sessionID, mode), nil
}
