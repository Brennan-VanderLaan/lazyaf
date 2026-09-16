package debugproto

// The escape key: client-side only, the server never sees it.
//
// EscapeByte is Ctrl-], the same escape `telnet` uses. NOT `@` sniffed out of
// the byte stream: scanning stdin for a leading `@` corrupts any program that
// legitimately reads `@...`, which is exactly why commands are their own
// frame type on the wire (C12). Ported from cli/lazyaf/debug_protocol.py:188-200.
const EscapeByte = 0x1d

// EscapeKeys: escape + key -> the verb the client sends. One table, so
// `@help` and the code cannot disagree about which keys exist.
var EscapeKeys = map[byte]string{
	'r': "@resume",
	'a': "@abort",
	's': "@status",
	'h': "@help",
	'?': "@help",
}

// EscapeDetachKeys: d / Ctrl-D leave the shell running and the session
// paused.
var EscapeDetachKeys = []byte{'d', 0x04}

// EscapeHelp is the one-line key legend printed on attach and on `@help`.
const EscapeHelp = "Ctrl-] then: r resume · a abort · s status · h help · d detach " +
	"(leaves the session paused) · Ctrl-] sends a literal Ctrl-]"

// ActionKind is what a chunk of keystrokes turned into.
type ActionKind int

const (
	// ActionStdin: Data goes to the shell as a stdin frame.
	ActionStdin ActionKind = iota
	// ActionCommand: Command is one of Commands, sent as a command frame.
	ActionCommand
	// ActionDetach: Key is the detach key pressed; the client exits locally.
	ActionDetach
	// ActionUnknown: Key followed the escape but means nothing. Never
	// silently eaten - the client reports it.
	ActionUnknown
)

// Action is one decoded keystroke outcome, in order.
type Action struct {
	Kind    ActionKind
	Data    []byte // ActionStdin
	Command string // ActionCommand
	Key     byte   // ActionDetach / ActionUnknown
}

// EscapeDecoder splits local keystrokes into stdin bytes, commands and a
// detach.
//
// A tiny state machine rather than an inline `if`, because it has to hold
// across chunk boundaries: a read can end exactly on the escape byte, and a
// client that forgot that would send the escape to the shell and then
// swallow the next real keystroke.
type EscapeDecoder struct {
	armed bool
}

// Armed is true when the previous chunk ended on the escape byte.
func (d *EscapeDecoder) Armed() bool { return d.armed }

func isDetachKey(b byte) bool {
	for _, k := range EscapeDetachKeys {
		if k == b {
			return true
		}
	}
	return false
}

// Feed decodes one chunk into the actions it carries, in order.
func (d *EscapeDecoder) Feed(data []byte) []Action {
	var actions []Action
	var pending []byte
	flush := func() {
		if len(pending) > 0 {
			actions = append(actions, Action{Kind: ActionStdin, Data: pending})
			pending = nil
		}
	}
	for _, b := range data {
		if d.armed {
			d.armed = false
			switch {
			case b == EscapeByte:
				pending = append(pending, EscapeByte) // doubled: send one literal
			case EscapeKeys[b] != "":
				flush()
				actions = append(actions, Action{Kind: ActionCommand, Command: EscapeKeys[b]})
			case isDetachKey(b):
				flush()
				actions = append(actions, Action{Kind: ActionDetach, Key: b})
			default:
				flush()
				actions = append(actions, Action{Kind: ActionUnknown, Key: b})
			}
			continue
		}
		if b == EscapeByte {
			d.armed = true
			continue
		}
		pending = append(pending, b)
	}
	flush()
	return actions
}
