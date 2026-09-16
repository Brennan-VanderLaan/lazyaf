package debugproto

import (
	"fmt"
	"strings"
)

// NoCloseCode is the `code` to pass CloseReason when the peer sent no code
// at all (Python's `None`). WebSocket close codes are positive, so -1 cannot
// collide with one.
const NoCloseCode = -1

// CloseReason is one sentence for a close, whatever the server managed to
// send. A refusal happens BEFORE the WebSocket is accepted, so its reason can
// only travel in the close frame - and an intermediary that drops the reason
// must not turn a stated refusal into a bare number.
// Port of cli/lazyaf/debug_protocol.py close_reason.
func CloseReason(code int, reason string) string {
	text := strings.TrimSpace(reason)
	if text != "" {
		if code != NoCloseCode {
			return fmt.Sprintf("%s (code %d)", text, code)
		}
		return text
	}
	if meaning, ok := CloseCodeMeanings[code]; ok {
		return fmt.Sprintf("%s (code %d)", meaning, code)
	}
	return fmt.Sprintf("the server closed the terminal with code %d", code)
}
