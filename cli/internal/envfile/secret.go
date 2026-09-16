package envfile

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
)

// Redacted is what a Secret prints as, under every verb and encoder.
const Redacted = "<redacted>"

// Secret is a generated value that cannot be printed.
//
// §8.1 asks for a type whose String/GoString/Format/MarshalText all answer
// "<redacted>" so that `lazyaf init` structurally cannot echo a value (the
// property test_bootstrap_secrets.py:191-214 pins). Those methods are here.
// One step further than the spec's `struct{ v string }`, stated: the bytes
// are NOT stored in the struct at all. fmt skips a value's methods when it
// reaches it through an unexported field (reflect.Value.CanInterface is
// false there), so a `%+v` of some future struct holding a Secret in an
// unexported field would have printed the string. A Secret is therefore
// only a handle into a package-private vault; a reflective dump shows
// `{id:3}`, never the value. WriteTo is the one way out, and write.go is
// its one caller.
type Secret struct {
	id uint64
}

var (
	vaultMu sync.Mutex
	vault   = map[uint64]string{}
	lastID  atomic.Uint64
)

// NewSecret wraps an existing value (the value a test seeds, say). Empty is
// allowed and writes nothing.
func NewSecret(value string) Secret {
	id := lastID.Add(1)
	vaultMu.Lock()
	vault[id] = value
	vaultMu.Unlock()
	return Secret{id: id}
}

// GenerateSecret is a cryptographically strong URL-safe value: SecretBytes
// of crypto/rand as unpadded URL-safe base64, 64 characters over the
// alphabet of Python's token_urlsafe (bootstrap_secrets.py:151-153).
func GenerateSecret() (Secret, error) {
	buf := make([]byte, SecretBytes)
	if _, err := io.ReadFull(rand.Reader, buf); err != nil {
		return Secret{}, fmt.Errorf("the system random source failed: %w", err)
	}
	return NewSecret(base64.RawURLEncoding.EncodeToString(buf)), nil
}

// IsZero is true for the zero Secret, which holds nothing.
func (s Secret) IsZero() bool { return s.id == 0 }

// WriteTo writes the value's bytes. It is the ONLY accessor, and the .env
// writer is its only caller in the binary.
func (s Secret) WriteTo(w io.Writer) (int64, error) {
	if s.id == 0 {
		return 0, errors.New("envfile: WriteTo on a zero Secret")
	}
	vaultMu.Lock()
	value, ok := vault[s.id]
	vaultMu.Unlock()
	if !ok {
		return 0, errors.New("envfile: Secret has no value")
	}
	n, err := io.WriteString(w, value)
	return int64(n), err
}

// Every printable surface answers Redacted.

func (s Secret) String() string   { return Redacted }
func (s Secret) GoString() string { return Redacted }

// Format satisfies fmt.Formatter, so %v %s %+v %#v %q %x and the rest all
// reach here rather than the default struct printer.
func (s Secret) Format(f fmt.State, verb rune) { _, _ = io.WriteString(f, Redacted) }

func (s Secret) MarshalText() ([]byte, error) { return []byte(Redacted), nil }
func (s Secret) MarshalJSON() ([]byte, error) { return []byte(`"` + Redacted + `"`), nil }
