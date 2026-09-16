// Package debugproto is the client half of the debug-terminal wire contract.
//
// The server half is backend/app/services/execution/debug_terminal.py. This
// package holds the same constants and the same encode/decode rules, and it
// is allowed to exist only because contract_test.go checks every one of them
// against tdd/contracts/debug_terminal.v1.json - a corpus only the SERVER can
// write (scripts/gen_debug_terminal_corpus.py imports the server codec; it
// cannot be regenerated from Go). upcoming/go-cli.md §3 is the design; §3.5
// is what goes red when either side moves.
//
// Stdlib only, on purpose: the binary embeds nothing from tdd/contracts and
// this package pulls in no network code, so `debug attach --print-credential`
// and every unit test work without a socket.
package debugproto

import "fmt"

// ProtocolVersion is the "v" every frame carries. A v2 is a new corpus file
// both consumers opt into, never a silent bump.
const ProtocolVersion = 1

// client -> server
const (
	TypeStdin   = "stdin"
	TypeResize  = "resize"
	TypeCommand = "command"
	TypePing    = "ping"
)

// server -> client
const (
	TypeReady  = "ready"
	TypeStdout = "stdout"
	TypeNotice = "notice"
	TypeClosed = "closed"
	TypePong   = "pong"
)

// ClientFrameTypes and ServerFrameTypes are sorted, like the corpus's
// constants.sets, so the contract test compares them positionally.
var (
	ClientFrameTypes = []string{TypeCommand, TypePing, TypeResize, TypeStdin}
	ServerFrameTypes = []string{TypeClosed, TypeNotice, TypePong, TypeReady, TypeStdout}
)

// Commands are the four verbs a `command` frame may carry, IN THE SERVER'S
// ORDER (the corpus pins COMMANDS as an array, §3.2). Every one is also a
// plain `lazyaf debug` subcommand, so controlling a session never depends on
// having a TTY.
var Commands = []string{"@resume", "@abort", "@status", "@help"}

// Close codes the server can answer an upgrade, or end a terminal, with.
const (
	CloseNormal            = 1000
	CloseBadToken          = 4401
	CloseNotAttachable     = 4403
	CloseUnknownSession    = 4404
	CloseDuplicateTerminal = 4004
	CloseBoundExceeded     = 4009
)

// CloseCodeMeanings is what the CLI prints when a proxy or an old server
// drops the close reason on the floor, so a refusal is never a bare number
// (R1). TestCloseDetails asserts every CLOSE_* the corpus lists has an entry.
var CloseCodeMeanings = map[int]string{
	CloseNormal:            "the terminal closed normally",
	CloseDuplicateTerminal: "a terminal is already attached to this debug session",
	CloseBoundExceeded:     "a protocol bound was exceeded (frame size, rate or output backlog)",
	CloseBadToken:          "missing or invalid join token - mint a new one with `lazyaf debug attach`",
	CloseNotAttachable:     "this session cannot be attached to right now",
	CloseUnknownSession:    "unknown debug session",
}

// Bounds. The client never restates them as literals elsewhere: the stdin
// chunk size (terminal.StdinChunkBytes) and the websocket read limit
// (terminal/dial.go) are both derived from MaxFrameBytes, so the corpus
// check on this one constant covers them (§3.5).
const (
	MaxFrameBytes          = 64 * 1024
	MaxOutboundQueue       = 256
	RateWindowSeconds      = 1.0
	RateMaxFramesPerWindow = 200
)

// ConnectionModeSidecar is the only attach mode at a breakpoint (§5).
const ConnectionModeSidecar = "sidecar"

// The two refusal sentences the server owns and the CLI repeats word for
// word (corpus constants.reasons). The Python CLI pinned its --shell refusal
// to the server's sentence (tdd/unit/scripts/test_cli_debug.py:560); the Go
// CLI keeps that pin through the contract test, which reads the sentence from
// data the server wrote.
const (
	ShellRefusedReason = "no step container exists at a pre-step breakpoint - the step has not " +
		"started. Use --sidecar to inspect the workspace it is about to run against."
	RemoteAttachReason = "terminal attach is not available for steps running on a remote runner " +
		"(12.7 ships local attach only)"
)

// ProtocolError is a frame that does not satisfy the contract. Every refusal
// this package makes is one of these and nothing else is (§3.4's stated
// deviation from Python's ValueError hierarchy), so the terminal client can
// tell "the server sent something I do not understand" from any other
// failure and print the former instead of dying.
type ProtocolError struct {
	Msg string
}

func (e *ProtocolError) Error() string { return e.Msg }

func protocolErrorf(format string, args ...any) error {
	return &ProtocolError{Msg: fmt.Sprintf(format, args...)}
}
