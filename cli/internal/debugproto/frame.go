package debugproto

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"strings"
	"unicode/utf8"
)

// Field is one key/value of a frame, in the order it goes on the wire.
//
// A slice of pairs rather than a map, and that is load-bearing: the contract
// test asserts Encode reproduces the server's bytes EXACTLY, and Python's
// json.dumps writes keys in insertion order. A Go map would shuffle them and
// the byte compare would be meaningless. A nil Value is dropped, exactly as
// the server drops None (`closed` with reason=None is `{"v":1,"type":"closed"}`).
type Field struct {
	Key   string
	Value any
}

// F is shorthand for building a Field inline.
func F(key string, value any) Field { return Field{Key: key, Value: value} }

func knownType(frameType string) bool {
	for _, t := range ClientFrameTypes {
		if t == frameType {
			return true
		}
	}
	for _, t := range ServerFrameTypes {
		if t == frameType {
			return true
		}
	}
	return false
}

// Encode serializes one frame to the text payload that goes on the wire:
// "v" first, "type" second, then the fields in the given order, compact
// separators, no HTML escaping (Python's json.dumps does not escape `<`).
//
// `data` fields are expected to be base64 ALREADY (use EncodeBytes); passing
// raw bytes here is a programming error and is refused, as the server refuses
// it (contract C12).
func Encode(frameType string, fields ...Field) (string, error) {
	if !knownType(frameType) {
		return "", protocolErrorf("unknown frame type %q", frameType)
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	buf.WriteString(`{"v":`)
	writeJSON(enc, &buf, ProtocolVersion)
	buf.WriteString(`,"type":`)
	writeJSON(enc, &buf, frameType)
	for _, f := range fields {
		if f.Value == nil {
			continue
		}
		if f.Key == "data" {
			switch f.Value.(type) {
			case []byte:
				return "", protocolErrorf("frame 'data' must be base64 text - call EncodeBytes() first (contract C12)")
			}
		}
		buf.WriteByte(',')
		writeJSON(enc, &buf, f.Key)
		buf.WriteByte(':')
		if err := writeJSON(enc, &buf, f.Value); err != nil {
			return "", protocolErrorf("field %q is not encodable: %v", f.Key, err)
		}
	}
	buf.WriteByte('}')
	return buf.String(), nil
}

// writeJSON appends one compact JSON value. json.Encoder always terminates a
// value with '\n'; it is trimmed here so the frame stays one compact line.
func writeJSON(enc *json.Encoder, buf *bytes.Buffer, v any) error {
	if err := enc.Encode(v); err != nil {
		return err
	}
	buf.Truncate(buf.Len() - 1)
	return nil
}

// Frame is one decoded, VALIDATED frame. Fields holds every key except "v"
// and "type"; numbers are json.Number so an int and a float stay distinct
// (a resize of 80.5 columns is a refusal, not a rounding).
type Frame struct {
	Type   string
	Fields map[string]any
}

// String returns a string field, or "" when absent or not a string - the
// same shape as Python's `frame.get("text", "")`.
func (f Frame) String(key string) string {
	s, _ := f.Fields[key].(string)
	return s
}

// Int returns an integer field. ok is false when absent, not an integer, or
// a bool/float pretending to be one.
func (f Frame) Int(key string) (int, bool) {
	n, ok := f.Fields[key].(json.Number)
	if !ok {
		return 0, false
	}
	v, err := n.Int64()
	if err != nil {
		return 0, false
	}
	return int(v), true
}

// Decode parses and VALIDATES one wire frame. It returns a *ProtocolError
// and never a partially-understood frame. The raw text is checked with
// utf8.Valid before JSON so a non-UTF-8 payload is refused by name, as the
// server refuses it, rather than being decoded into replacement runes.
func Decode(raw string) (Frame, error) {
	if !utf8.ValidString(raw) {
		return Frame{}, protocolErrorf("frame is not UTF-8 text")
	}
	dec := json.NewDecoder(strings.NewReader(raw))
	dec.UseNumber()
	var payload any
	if err := dec.Decode(&payload); err != nil {
		return Frame{}, protocolErrorf("frame is not JSON: %v", err)
	}
	if dec.More() {
		return Frame{}, protocolErrorf("frame is not JSON: trailing data after the object")
	}
	obj, ok := payload.(map[string]any)
	if !ok {
		return Frame{}, protocolErrorf("frame must be a JSON object")
	}
	// Numeric compare, not string: Python's `frame.get("v") != 1` accepts
	// a 1.0, so this does too. A missing or non-numeric "v" is refused.
	if v, ok := obj["v"].(json.Number); !ok || !isVersion(v) {
		return Frame{}, protocolErrorf("unsupported protocol version %v (this client speaks v%d)", obj["v"], ProtocolVersion)
	}
	frameType, _ := obj["type"].(string)
	if !knownType(frameType) {
		return Frame{}, protocolErrorf("unknown frame type %q", frameType)
	}
	frame := Frame{Type: frameType, Fields: make(map[string]any, len(obj))}
	for k, v := range obj {
		if k != "v" && k != "type" {
			frame.Fields[k] = v
		}
	}

	switch frameType {
	case TypeStdin, TypeStdout:
		data, ok := frame.Fields["data"].(string)
		if !ok {
			return Frame{}, protocolErrorf("%s frame needs a base64 'data' string", frameType)
		}
		// Decode eagerly: a frame that cannot round-trip is refused here
		// rather than corrupting the terminal downstream.
		if _, err := DecodeBytes(data); err != nil {
			return Frame{}, err
		}
	case TypeResize:
		for _, field := range []string{"cols", "rows"} {
			n, ok := frame.Int(field)
			if !ok || n <= 0 {
				return Frame{}, protocolErrorf("resize frame needs a positive int %q", field)
			}
		}
	case TypeCommand:
		command, _ := frame.Fields["command"].(string)
		known := false
		for _, c := range Commands {
			if c == command {
				known = true
			}
		}
		if !known {
			return Frame{}, protocolErrorf("unknown command %q (known: %s)", command, strings.Join(Commands, ", "))
		}
	}
	return frame, nil
}

func isVersion(n json.Number) bool {
	f, err := n.Float64()
	return err == nil && f == ProtocolVersion
}

// EncodeBytes: raw terminal bytes -> the base64 text a `data` field carries.
func EncodeBytes(data []byte) string {
	return base64.StdEncoding.EncodeToString(data)
}

// DecodeBytes: the base64 text a `data` field carries -> raw terminal bytes.
// Strict, like Python's validate=True: "A", "not!base64" and "@@@" are
// refusals, not best-effort decodes.
//
// Go's Strict() is not quite validate=True: it still IGNORES CR and LF
// anywhere in the input (encoding/base64 docs: "the input is still
// malleable, as new line characters are still ignored"), so "YQ==\n" would
// decode to "a" here and be a DebugProtocolError on the server. That gap is
// closed below by refusing CR/LF before the decoder sees them (verifier
// finding V1-2, pinned by TestDecodeBytesRefusesNewlinesLikeValidateTrue).
// The ONE remaining stated deviation is the other direction: Go's Strict
// refuses non-canonical trailing bits ("YR=="), which Python's validate=True
// lets through; every `data` either side emits is canonical, so nothing on
// the wire can hit it, and the test pins it so a change is noticed.
func DecodeBytes(data string) ([]byte, error) {
	if strings.ContainsAny(data, "\r\n") {
		return nil, protocolErrorf("'data' is not valid base64: newline in payload")
	}
	out, err := base64.StdEncoding.Strict().DecodeString(data)
	if err != nil {
		return nil, protocolErrorf("'data' is not valid base64: %v", err)
	}
	return out, nil
}
