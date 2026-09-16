package debugproto

// The Go side of the codec contract (upcoming/go-cli.md §3.4).
//
// tdd/contracts/debug_terminal.v1.json is written ONLY by
// scripts/gen_debug_terminal_corpus.py, which imports the SERVER codec; T1's
// test_terminal_protocol_contract.py proves the committed file equals a fresh
// export. So everything below is Go being tested against bytes the server
// produced - the corpus cannot be regenerated from Go, by design (§3.5).
//
// The corpus is read from the checkout at test time, never embedded: the
// shipped binary does not need it, and //go:embed cannot reach ../../../
// anyway (§2.3). A missing corpus is t.Fatalf with the absolute path - never
// t.Skip (R4).

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
)

const corpusRelPath = "../../../tdd/contracts/debug_terminal.v1.json"

type corpusFrame struct {
	Name      string              `json:"name"`
	Direction string              `json:"direction"`
	Type      string              `json:"type"`
	Fields    [][]json.RawMessage `json:"fields"`
	DataHex   *string             `json:"data_hex"`
	Wire      string              `json:"wire"`
}

type corpusVector struct {
	BytesHex string `json:"bytes_hex"`
	Text     string `json:"text"`
}

type corpus struct {
	Base64 struct {
		Encode []corpusVector `json:"encode"`
		Decode []corpusVector `json:"decode"`
		Reject []string       `json:"reject"`
	}
	Constants struct {
		DerivedTypes []string                   `json:"derived_types"`
		Reasons      map[string]string          `json:"reasons"`
		Scalars      map[string]json.RawMessage `json:"scalars"`
		Sets         map[string][]string        `json:"sets"`
	}
	Frames    []corpusFrame
	Malformed []map[string]json.RawMessage
	// sections is every top-level key and every key under "constants", so the
	// unknown-key check covers a section the generator grows too.
	sections         []string
	constantSections []string
}

func loadCorpus(t *testing.T) *corpus {
	t.Helper()
	abs, err := filepath.Abs(corpusRelPath)
	if err != nil {
		t.Fatalf("resolving %s: %v", corpusRelPath, err)
	}
	raw, err := os.ReadFile(abs)
	if err != nil {
		t.Fatalf("the codec corpus is missing at %s (%v) - run from a LazyAF checkout; "+
			"it is written by `python scripts/gen_debug_terminal_corpus.py`", abs, err)
	}
	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		t.Fatalf("%s is not a JSON object: %v", abs, err)
	}
	c := &corpus{}
	for k := range top {
		c.sections = append(c.sections, k)
	}
	sort.Strings(c.sections)
	must := func(key string, into any) {
		section, ok := top[key]
		if !ok {
			t.Fatalf("%s has no %q section", abs, key)
		}
		if err := json.Unmarshal(section, into); err != nil {
			t.Fatalf("%s section %q: %v", abs, key, err)
		}
	}
	must("base64", &c.Base64)
	must("constants", &c.Constants)
	must("frames", &c.Frames)
	must("malformed", &c.Malformed)
	var constants map[string]json.RawMessage
	must("constants", &constants)
	for k := range constants {
		c.constantSections = append(c.constantSections, k)
	}
	sort.Strings(c.constantSections)
	return c
}

// jsonEqual compares a corpus value with a Go value through JSON, so
// Python's `1.0` and Go's float64(1) agree and a []string matches an array.
func jsonEqual(raw json.RawMessage, goValue any) bool {
	var want any
	if err := json.Unmarshal(raw, &want); err != nil {
		return false
	}
	encoded, err := json.Marshal(goValue)
	if err != nil {
		return false
	}
	var got any
	if err := json.Unmarshal(encoded, &got); err != nil {
		return false
	}
	return reflect.DeepEqual(want, got)
}

func unhex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("corpus hex %q: %v", s, err)
	}
	return b
}

// goScalars is the table from every JSON key the corpus's constants.scalars
// holds to the Go value that must equal it. A key the corpus has and this
// table lacks fails TestContractConstants by name, so a new server constant
// fails Go rather than being ignored.
var goScalars = map[string]any{
	"PROTOCOL_VERSION":           ProtocolVersion,
	"TYPE_STDIN":                 TypeStdin,
	"TYPE_RESIZE":                TypeResize,
	"TYPE_COMMAND":               TypeCommand,
	"TYPE_PING":                  TypePing,
	"TYPE_READY":                 TypeReady,
	"TYPE_STDOUT":                TypeStdout,
	"TYPE_NOTICE":                TypeNotice,
	"TYPE_CLOSED":                TypeClosed,
	"TYPE_PONG":                  TypePong,
	"COMMANDS":                   Commands,
	"CLOSE_NORMAL":               CloseNormal,
	"CLOSE_BAD_TOKEN":            CloseBadToken,
	"CLOSE_NOT_ATTACHABLE":       CloseNotAttachable,
	"CLOSE_UNKNOWN_SESSION":      CloseUnknownSession,
	"CLOSE_DUPLICATE_TERMINAL":   CloseDuplicateTerminal,
	"CLOSE_BOUND_EXCEEDED":       CloseBoundExceeded,
	"MAX_FRAME_BYTES":            MaxFrameBytes,
	"MAX_OUTBOUND_QUEUE":         MaxOutboundQueue,
	"RATE_WINDOW_SECONDS":        RateWindowSeconds,
	"RATE_MAX_FRAMES_PER_WINDOW": RateMaxFramesPerWindow,
	"CONNECTION_MODE_SIDECAR":    ConnectionModeSidecar,
}

var goSets = map[string][]string{
	"CLIENT_FRAME_TYPES": ClientFrameTypes,
	"SERVER_FRAME_TYPES": ServerFrameTypes,
}

var goReasons = map[string]string{
	"SHELL_REFUSED_REASON": ShellRefusedReason,
	"REMOTE_ATTACH_REASON": RemoteAttachReason,
}

func TestContractConstants(t *testing.T) {
	c := loadCorpus(t)
	for name, raw := range c.Constants.Scalars {
		t.Run("scalar/"+name, func(t *testing.T) {
			goValue, ok := goScalars[name]
			if !ok {
				t.Fatalf("unknown contract constant %s - add it to debugproto", name)
			}
			if !jsonEqual(raw, goValue) {
				t.Fatalf("%s: corpus %s, Go %v", name, raw, goValue)
			}
		})
	}
	for name := range goScalars {
		if _, ok := c.Constants.Scalars[name]; !ok {
			t.Errorf("Go pins scalar %s that the corpus no longer lists - the server dropped it", name)
		}
	}
	for name, want := range c.Constants.Sets {
		t.Run("set/"+name, func(t *testing.T) {
			got, ok := goSets[name]
			if !ok {
				t.Fatalf("unknown contract constant %s - add it to debugproto", name)
			}
			if !reflect.DeepEqual(want, got) {
				t.Fatalf("%s: corpus %v, Go %v", name, want, got)
			}
		})
	}
	for name := range goSets {
		if _, ok := c.Constants.Sets[name]; !ok {
			t.Errorf("Go pins set %s that the corpus no longer lists", name)
		}
	}
	// COMMANDS is an ARRAY: order matters (§3.2), and jsonEqual above already
	// compared it positionally; this makes the property greppable by name.
	var commands []string
	if err := json.Unmarshal(c.Constants.Scalars["COMMANDS"], &commands); err != nil {
		t.Fatalf("COMMANDS: %v", err)
	}
	if !reflect.DeepEqual(commands, Commands) {
		t.Fatalf("Commands order: corpus %v, Go %v", commands, Commands)
	}
}

func TestContractReasons(t *testing.T) {
	c := loadCorpus(t)
	for name, want := range c.Constants.Reasons {
		t.Run(name, func(t *testing.T) {
			got, ok := goReasons[name]
			if !ok {
				t.Fatalf("unknown contract constant %s - add it to debugproto", name)
			}
			if got != want {
				t.Fatalf("%s:\n corpus %q\n Go     %q", name, want, got)
			}
		})
	}
	for name := range goReasons {
		if _, ok := c.Constants.Reasons[name]; !ok {
			t.Errorf("Go pins reason %s that the corpus no longer lists", name)
		}
	}
}

func TestContractDerivedTypes(t *testing.T) {
	// The server's every TYPE_* attribute; a frame type added there without
	// a Go twin lands here by name.
	c := loadCorpus(t)
	want := append([]string(nil), c.Constants.DerivedTypes...)
	sort.Strings(want)
	got := append(append([]string(nil), ClientFrameTypes...), ServerFrameTypes...)
	sort.Strings(got)
	if !reflect.DeepEqual(want, got) {
		t.Fatalf("derived TYPE_* set: corpus %v, Go %v - add the missing type to debugproto", want, got)
	}
}

func TestContractHasNoUnknownSections(t *testing.T) {
	c := loadCorpus(t)
	known := map[string]bool{"base64": true, "constants": true, "frames": true, "malformed": true}
	for _, s := range c.sections {
		if !known[s] {
			t.Errorf("unknown contract section %q - add it to contract_test.go", s)
		}
	}
	knownConstants := map[string]bool{"derived_types": true, "reasons": true, "scalars": true, "sets": true}
	for _, s := range c.constantSections {
		if !knownConstants[s] {
			t.Errorf("unknown contract constant section %q - add it to contract_test.go", s)
		}
	}
}

// fieldsOf turns the corpus's ordered [[key, value], ...] pairs into the
// Fields Encode takes, keeping order and turning JSON null into a nil Value.
func fieldsOf(t *testing.T, pairs [][]json.RawMessage) []Field {
	t.Helper()
	var fields []Field
	for _, pair := range pairs {
		if len(pair) != 2 {
			t.Fatalf("corpus field pair has %d elements, want 2", len(pair))
		}
		var key string
		if err := json.Unmarshal(pair[0], &key); err != nil {
			t.Fatalf("corpus field key %s: %v", pair[0], err)
		}
		var value any
		dec := json.NewDecoder(strings.NewReader(string(pair[1])))
		dec.UseNumber()
		if err := dec.Decode(&value); err != nil {
			t.Fatalf("corpus field value %s: %v", pair[1], err)
		}
		if n, ok := value.(json.Number); ok {
			if i, err := n.Int64(); err == nil {
				value = i
			} else {
				f, _ := n.Float64()
				value = f
			}
		}
		fields = append(fields, Field{Key: key, Value: value})
	}
	return fields
}

func TestContractFrames(t *testing.T) {
	c := loadCorpus(t)
	if len(c.Frames) == 0 {
		t.Fatal("the corpus lists no frames")
	}
	for _, tc := range c.Frames {
		t.Run(tc.Name, func(t *testing.T) {
			fields := fieldsOf(t, tc.Fields)

			// Encode is generic, so the server's frames are re-encoded too:
			// a free extra pin on key order and separators (§3.4).
			wire, err := Encode(tc.Type, fields...)
			if err != nil {
				t.Fatalf("Encode(%s): %v", tc.Name, err)
			}
			if wire != tc.Wire {
				t.Fatalf("Encode(%s) is not byte-exact\n got  %s\n want %s", tc.Name, wire, tc.Wire)
			}

			frame, err := Decode(tc.Wire)
			if err != nil {
				t.Fatalf("Decode(%s): %v", tc.Name, err)
			}
			if frame.Type != tc.Type {
				t.Fatalf("Decode(%s).Type = %q, want %q", tc.Name, frame.Type, tc.Type)
			}
			present := 0
			for _, f := range fields {
				got, ok := frame.Fields[f.Key]
				if f.Value == nil {
					if ok {
						t.Fatalf("Decode(%s): field %q should be absent (None is dropped), got %v", tc.Name, f.Key, got)
					}
					continue
				}
				present++
				if !ok {
					t.Fatalf("Decode(%s): field %q missing", tc.Name, f.Key)
				}
				if n, isNum := got.(json.Number); isNum {
					if !jsonEqual(json.RawMessage(n.String()), f.Value) {
						t.Fatalf("Decode(%s): field %q = %v, want %v", tc.Name, f.Key, got, f.Value)
					}
				} else if !reflect.DeepEqual(got, f.Value) {
					t.Fatalf("Decode(%s): field %q = %#v, want %#v", tc.Name, f.Key, got, f.Value)
				}
			}
			if len(frame.Fields) != present {
				t.Fatalf("Decode(%s) has %d fields, the corpus lists %d", tc.Name, len(frame.Fields), present)
			}

			if tc.DataHex != nil {
				want := unhex(t, *tc.DataHex)
				got, err := DecodeBytes(frame.String("data"))
				if err != nil {
					t.Fatalf("DecodeBytes(%s): %v", tc.Name, err)
				}
				if string(got) != string(want) {
					t.Fatalf("DecodeBytes(%s) is not byte-exact", tc.Name)
				}
				if EncodeBytes(want) != frame.String("data") {
					t.Fatalf("EncodeBytes(%s) does not reproduce the corpus text", tc.Name)
				}
			}
		})
	}
}

func TestContractFramesCoverEveryType(t *testing.T) {
	// A corpus that silently shrinks would lower the executed count into the
	// floor (R4); this makes the shrink a named failure as well.
	c := loadCorpus(t)
	seen := map[string]bool{}
	for _, f := range c.Frames {
		seen[f.Type] = true
		if f.Direction != "server->client" && f.Direction != "client->server" {
			t.Errorf("frame %s has direction %q", f.Name, f.Direction)
		}
	}
	for _, typ := range c.Constants.DerivedTypes {
		if !seen[typ] {
			t.Errorf("no frame case for type %q", typ)
		}
	}
}

func asProtocolError(t *testing.T, what string, err error) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s: accepted, want a *ProtocolError refusal", what)
	}
	var pe *ProtocolError
	if !errors.As(err, &pe) {
		t.Fatalf("%s: refused with %T (%v), want *ProtocolError - every refusal is one and nothing else is", what, err, err)
	}
}

func TestContractMalformed(t *testing.T) {
	c := loadCorpus(t)
	if len(c.Malformed) == 0 {
		t.Fatal("the corpus lists no malformed cases")
	}
	for i, entry := range c.Malformed {
		name := "entry-" + strconv.Itoa(i)
		if raw, ok := entry["name"]; ok {
			_ = json.Unmarshal(raw, &name)
		}
		switch {
		case entry["raw"] != nil:
			t.Run(name, func(t *testing.T) {
				var raw string
				if err := json.Unmarshal(entry["raw"], &raw); err != nil {
					t.Fatalf("corpus raw: %v", err)
				}
				_, err := Decode(raw)
				asProtocolError(t, "Decode("+raw+")", err)
			})
		case entry["raw_hex"] != nil:
			t.Run(name, func(t *testing.T) {
				var rawHex string
				if err := json.Unmarshal(entry["raw_hex"], &rawHex); err != nil {
					t.Fatalf("corpus raw_hex: %v", err)
				}
				_, err := Decode(string(unhex(t, rawHex)))
				asProtocolError(t, "Decode(hex "+rawHex+")", err)
			})
		case entry["encode_unknown_type"] != nil:
			t.Run("encode-unknown-type", func(t *testing.T) {
				var typ string
				if err := json.Unmarshal(entry["encode_unknown_type"], &typ); err != nil {
					t.Fatalf("corpus encode_unknown_type: %v", err)
				}
				_, err := Encode(typ)
				asProtocolError(t, "Encode("+typ+")", err)
			})
		case entry["encode_raw_bytes_in_data"] != nil:
			t.Run("encode-raw-bytes-in-data", func(t *testing.T) {
				_, err := Encode(TypeStdin, F("data", []byte("raw")))
				asProtocolError(t, "Encode(stdin, data=[]byte)", err)
			})
		default:
			t.Fatalf("malformed entry %d has a shape this test does not know: %v", i, entry)
		}
	}
}

func TestContractRefusalsAreProtocolErrorsAndNothingElseIs(t *testing.T) {
	// The Go reading of `:250-253` (DebugProtocolError is a ValueError):
	// every frame the corpus calls valid decodes with a nil error.
	c := loadCorpus(t)
	for _, f := range c.Frames {
		if _, err := Decode(f.Wire); err != nil {
			t.Errorf("Decode(%s) refused a valid frame: %v", f.Name, err)
		}
	}
	for _, v := range c.Base64.Decode {
		if _, err := DecodeBytes(v.Text); err != nil {
			t.Errorf("DecodeBytes(%q) refused a valid vector: %v", v.Text, err)
		}
	}
}

func TestContractBase64(t *testing.T) {
	c := loadCorpus(t)
	if len(c.Base64.Encode) == 0 || len(c.Base64.Decode) == 0 || len(c.Base64.Reject) == 0 {
		t.Fatalf("base64 section is incomplete: %d encode, %d decode, %d reject",
			len(c.Base64.Encode), len(c.Base64.Decode), len(c.Base64.Reject))
	}
	for i, v := range c.Base64.Encode {
		t.Run("encode/"+strconv.Itoa(i), func(t *testing.T) {
			if got := EncodeBytes(unhex(t, v.BytesHex)); got != v.Text {
				t.Fatalf("EncodeBytes(hex %s) = %q, want %q", v.BytesHex, got, v.Text)
			}
		})
	}
	for i, v := range c.Base64.Decode {
		t.Run("decode/"+strconv.Itoa(i), func(t *testing.T) {
			got, err := DecodeBytes(v.Text)
			if err != nil {
				t.Fatalf("DecodeBytes(%q): %v", v.Text, err)
			}
			if hex.EncodeToString(got) != v.BytesHex {
				t.Fatalf("DecodeBytes(%q) = %x, want %s", v.Text, got, v.BytesHex)
			}
		})
	}
	for _, text := range c.Base64.Reject {
		t.Run("reject/"+text, func(t *testing.T) {
			_, err := DecodeBytes(text)
			asProtocolError(t, "DecodeBytes("+text+")", err)
		})
	}
}

func TestEscapeKeysMapToWireCommands(t *testing.T) {
	c := loadCorpus(t)
	var commands []string
	if err := json.Unmarshal(c.Constants.Scalars["COMMANDS"], &commands); err != nil {
		t.Fatalf("COMMANDS: %v", err)
	}
	want := map[string]bool{}
	for _, cmd := range commands {
		want[cmd] = true
	}
	got := map[string]bool{}
	for _, verb := range EscapeKeys {
		got[verb] = true
	}
	if !reflect.DeepEqual(want, got) {
		t.Fatalf("escape keys reach %v, the wire's verbs are %v", got, want)
	}
	for _, cmd := range commands {
		reachable := false
		for _, verb := range EscapeKeys {
			if verb == cmd {
				reachable = true
			}
		}
		if !reachable {
			t.Errorf("verb %s has no escape key", cmd)
		}
	}
}

func TestEscapeSurface(t *testing.T) {
	// debug_protocol.py:188-200: Ctrl-], the five keys, the two detach keys.
	if EscapeByte != 0x1d {
		t.Fatalf("EscapeByte = %#x, want 0x1d (Ctrl-])", EscapeByte)
	}
	want := map[byte]string{'r': "@resume", 'a': "@abort", 's': "@status", 'h': "@help", '?': "@help"}
	if !reflect.DeepEqual(EscapeKeys, want) {
		t.Fatalf("EscapeKeys = %v, want %v", EscapeKeys, want)
	}
	if !reflect.DeepEqual(EscapeDetachKeys, []byte{'d', 0x04}) {
		t.Fatalf("EscapeDetachKeys = %v, want d and Ctrl-D", EscapeDetachKeys)
	}
	for _, key := range []string{"r resume", "a abort", "s status", "h help", "d detach"} {
		if !strings.Contains(EscapeHelp, key) {
			t.Errorf("EscapeHelp does not mention %q", key)
		}
	}
}

// --- the decoder (tdd/unit/scripts/test_cli_debug.py::TestEscapeDecoder) ---

func stdin(b string) Action    { return Action{Kind: ActionStdin, Data: []byte(b)} }
func command(v string) Action  { return Action{Kind: ActionCommand, Command: v} }
func detach(k byte) Action     { return Action{Kind: ActionDetach, Key: k} }
func unknownKey(k byte) Action { return Action{Kind: ActionUnknown, Key: k} }
func feed(s string) []Action   { d := &EscapeDecoder{}; return d.Feed([]byte(s)) }
func same(a, b []Action) bool  { return reflect.DeepEqual(a, b) }
func show(a []Action) string {
	return strings.TrimSpace(strings.ReplaceAll(sprintActions(a), "\n", " "))
}
func sprintActions(a []Action) string {
	var sb strings.Builder
	for _, x := range a {
		switch x.Kind {
		case ActionStdin:
			sb.WriteString("stdin(" + string(x.Data) + ") ")
		case ActionCommand:
			sb.WriteString("command(" + x.Command + ") ")
		case ActionDetach:
			sb.WriteString("detach(" + string(x.Key) + ") ")
		case ActionUnknown:
			sb.WriteString("unknown(" + string(x.Key) + ") ")
		}
	}
	return sb.String()
}

func TestEscapeDecoder(t *testing.T) {
	t.Run("plain bytes pass through untouched", func(t *testing.T) {
		if got := feed("ls -la\n"); !same(got, []Action{stdin("ls -la\n")}) {
			t.Fatalf("got %s", show(got))
		}
	})
	t.Run("an at sign in the byte stream is just input", func(t *testing.T) {
		// The whole reason commands are their own frame type (C12): a
		// program reading `@resume` from its own stdin must receive it.
		if got := feed("@resume\n"); !same(got, []Action{stdin("@resume\n")}) {
			t.Fatalf("got %s", show(got))
		}
	})
	t.Run("escape plus key becomes a command", func(t *testing.T) {
		for key, verb := range map[string]string{"r": "@resume", "a": "@abort", "s": "@status", "h": "@help", "?": "@help"} {
			if got := feed("\x1d" + key); !same(got, []Action{command(verb)}) {
				t.Fatalf("key %q: got %s", key, show(got))
			}
		}
	})
	t.Run("doubled escape sends one literal escape byte", func(t *testing.T) {
		if got := feed("\x1d\x1d"); !same(got, []Action{stdin("\x1d")}) {
			t.Fatalf("got %s", show(got))
		}
	})
	t.Run("escape split across two reads still works", func(t *testing.T) {
		// The bug a naive per-chunk scan ships with: a read that ends exactly
		// on the escape byte sends it to the shell and eats the next keystroke.
		d := &EscapeDecoder{}
		if got := d.Feed([]byte("echo hi\x1d")); !same(got, []Action{stdin("echo hi")}) {
			t.Fatalf("first chunk: got %s", show(got))
		}
		if !d.Armed() {
			t.Fatal("decoder must stay armed across the chunk boundary")
		}
		if got := d.Feed([]byte("r")); !same(got, []Action{command("@resume")}) {
			t.Fatalf("second chunk: got %s", show(got))
		}
	})
	t.Run("input before and after a command keeps its order", func(t *testing.T) {
		want := []Action{stdin("ab"), command("@status"), stdin("cd")}
		if got := feed("ab\x1dscd"); !same(got, want) {
			t.Fatalf("got %s", show(got))
		}
	})
	t.Run("detach keys", func(t *testing.T) {
		for _, key := range []byte{'d', 0x04} {
			if got := feed("\x1d" + string(key)); !same(got, []Action{detach(key)}) {
				t.Fatalf("key %#x: got %s", key, show(got))
			}
		}
	})
	t.Run("an unknown escape key is reported not swallowed", func(t *testing.T) {
		if got := feed("\x1dz"); !same(got, []Action{unknownKey('z')}) {
			t.Fatalf("got %s", show(got))
		}
	})
}
