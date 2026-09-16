package debugproto

import (
	"errors"
	"testing"
)

// TestDecodeBytesRefusesNewlinesLikeValidateTrue pins the base64 gap the
// corpus does not reach (verifier finding V1-2): Go's Strict() decoder
// ignores CR and LF, Python's validate=True refuses them. Both decoders must
// agree on every byte sequence that can arrive in a `data` field, so the
// Go side refuses them too. The vectors are the ones the finding was proven
// with; the corpus's own base64.reject list is the server's to extend
// (requested of L1) and TestContractBase64 will pick that up by data.
func TestDecodeBytesRefusesNewlinesLikeValidateTrue(t *testing.T) {
	for _, text := range []string{"YQ==\n", "YQ==\r\n", "YQ==\n\n", "\nYQ==", "YQ\n==", "\r"} {
		t.Run(text, func(t *testing.T) {
			_, err := DecodeBytes(text)
			asProtocolError(t, "DecodeBytes", err)
		})
	}
	t.Run("through Decode: a stdout frame with a JSON-escaped newline in data", func(t *testing.T) {
		// The exact wire from the finding: the server's decode_frame raises
		// "'data' is not valid base64: Non-base64 digit found" on it.
		_, err := Decode(`{"v":1,"type":"stdout","data":"YQ==\n"}`)
		asProtocolError(t, "Decode", err)
		_, err = Decode(`{"v":1,"type":"stdin","data":"\r\nYQ=="}`)
		asProtocolError(t, "Decode", err)
	})
	t.Run("the canonical form still decodes", func(t *testing.T) {
		got, err := DecodeBytes("YQ==")
		if err != nil || string(got) != "a" {
			t.Fatalf("DecodeBytes(\"YQ==\") = %q, %v; want \"a\", nil", got, err)
		}
		if _, err := Decode(`{"v":1,"type":"stdout","data":"YQ=="}`); err != nil {
			t.Fatalf("Decode refused the canonical frame: %v", err)
		}
	})
}

// TestDecodeBytesStatedDeviationNonCanonicalTrailingBits pins the ONE
// deviation frame.go still states: Go refuses "YR==" (non-canonical
// trailing bits), Python's validate=True accepts it as b"a". Nothing either
// side emits is non-canonical, so the wire cannot hit it - but if Go's
// behaviour ever changes, this goes red and the comment in frame.go is
// revisited instead of silently drifting.
func TestDecodeBytesStatedDeviationNonCanonicalTrailingBits(t *testing.T) {
	_, err := DecodeBytes("YR==")
	var pe *ProtocolError
	if !errors.As(err, &pe) {
		t.Fatalf("DecodeBytes(\"YR==\") = %v; Go's Strict() was expected to refuse "+
			"non-canonical trailing bits - if it no longer does, update the "+
			"stated deviation in frame.go", err)
	}
}
