// Package envfile is the .env model shared by `lazyaf init` and `lazyaf
// doctor` (upcoming/go-cli.md §2.3, §8.1, §8.2).
//
// It is ONE parser on purpose. The two Python scripts it replaces each carried
// their own (scripts/bootstrap_secrets.py:164-190 and scripts/preflight.py:
// 173-194), kept identical by eye; a divergence there would have meant init
// writing a value doctor could not read. Here ReadValues is the only reader,
// and both commands call it (§8.2, "ONE .env parser shared with init").
//
// The other thing this package owns is the Secret type: a generated value
// that no fmt verb, no JSON encoder and no reflective dump can print
// (secret.go). Only the writer in write.go ever sees its bytes.
package envfile

import (
	"os"
	"runtime"
	"strings"
)

// platformNewline is os.linesep: what a brand-new file gets when there is
// no existing ending to preserve.
var platformNewline = func() string {
	if runtime.GOOS == "windows" {
		return "\r\n"
	}
	return "\n"
}()

// ManagedSecret is one of the keys `lazyaf init` fills in. Purpose is written
// into .env as a comment above the generated line, so someone reading the
// file later knows what they are looking at without going to find the docs
// (bootstrap_secrets.py:75-87).
type ManagedSecret struct {
	Key     string
	Purpose string
}

// ManagedSecrets are the keys the backend refuses to start without
// (bootstrap_secrets.py:78-87). In this order, so the appended block reads
// the same as the one the Python script wrote.
var ManagedSecrets = []ManagedSecret{
	{"LAZYAF_STEP_AUTH_SECRET", "signs the short-lived JWT a step container uses to call /api/steps/*"},
	{"LAZYAF_RUNNER_AUTH_SECRET", "shared enrollment secret a runner agent presents at /ws/runner"},
}

// SecretBytes is the entropy per generated secret. 48 bytes URL-safe base64
// without padding is 64 characters - token_urlsafe(48), so a .env written by
// the Python script and one written here are indistinguishable
// (bootstrap_secrets.py:72-73, :151-153).
const SecretBytes = 48

// SplitAssignment is (key, value, true) for an ACTIVE assignment line, else
// ("", "", false). Ported from bootstrap_secrets.py:158-180.
//
// Commented lines are deliberately not assignments: `# LAZYAF_X=` is
// documentation, and replacing it in place would be an edit nobody asked
// for. A missing key gets a fresh block appended instead. `export KEY=v` is
// accepted, and one layer of matching quotes is stripped from the value.
func SplitAssignment(line string) (key, value string, ok bool) {
	stripped := strings.TrimSpace(line)
	if stripped == "" || strings.HasPrefix(stripped, "#") || !strings.Contains(stripped, "=") {
		return "", "", false
	}
	key, value, _ = strings.Cut(stripped, "=")
	key = strings.TrimSpace(key)
	if strings.HasPrefix(key, "export ") {
		key = strings.TrimSpace(strings.TrimPrefix(key, "export "))
	}
	if key == "" {
		return "", "", false
	}
	return key, stripQuotes(strings.TrimSpace(value)), true
}

func stripQuotes(v string) string {
	if len(v) >= 2 && v[0] == v[len(v)-1] && (v[0] == '"' || v[0] == '\'') {
		return v[1 : len(v)-1]
	}
	return v
}

// ReadValues is {KEY: value} from the active assignments in lines. The last
// occurrence wins, as dotenv reads it (bootstrap_secrets.py:183-190).
func ReadValues(lines []string) map[string]string {
	values := map[string]string{}
	for _, line := range lines {
		if key, value, ok := SplitAssignment(line); ok {
			values[key] = value
		}
	}
	return values
}

// DetectNewline is the file's own line ending, so a rewrite preserves it
// instead of imposing one (bootstrap_secrets.py:193-199). A file with no
// newline at all gets the platform's.
func DetectNewline(raw string) string {
	if strings.Contains(raw, "\r\n") {
		return "\r\n"
	}
	if strings.Contains(raw, "\n") {
		return "\n"
	}
	return platformNewline
}

// SplitLines is Python's str.splitlines for the two endings a .env can hold:
// CRLF and LF, with no trailing empty element for a file that ends in a
// newline. A lone CR inside a line is left alone - it is content, not an
// ending we would ever write.
func SplitLines(raw string) []string {
	if raw == "" {
		return nil
	}
	parts := strings.Split(raw, "\n")
	if parts[len(parts)-1] == "" {
		parts = parts[:len(parts)-1]
	}
	for i, p := range parts {
		parts[i] = strings.TrimSuffix(p, "\r")
	}
	return parts
}

// Load is (lines, newline) for an existing file, or (nil, platform newline)
// when it does not exist. Any other error is returned: an unreadable .env is
// a fact the operator needs, not something to treat as empty.
func Load(path string) (lines []string, newline string, err error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, platformNewline, nil
		}
		return nil, "", err
	}
	return SplitLines(string(raw)), DetectNewline(string(raw)), nil
}

// ReadFileValues is ReadValues over a file on disk - the single call both
// commands make. A missing file is an error the caller reports; it is never
// an empty map, because "no .env" and "an empty .env" get different advice.
func ReadFileValues(path string) (map[string]string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ReadValues(SplitLines(string(raw))), nil
}

// IsPlaceholder is true when a value means "not filled in" rather than a
// real secret (bootstrap_secrets.py:134-148, identical to preflight.py:
// 312-324): blank, a retired public default (exact), a placeholder shape
// (case-insensitive, from the generated table), or nothing but x's.
func IsPlaceholder(value string) bool {
	candidate := strings.TrimSpace(value)
	if candidate == "" {
		return true
	}
	if IsRetiredPublicSecret(candidate) {
		return true
	}
	lowered := strings.ToLower(candidate)
	for _, p := range PlaceholderSecrets {
		if lowered == p {
			return true
		}
	}
	return strings.Trim(lowered, "x") == ""
}

// IsRetiredPublicSecret is true for a constant LazyAF used to ship
// (placeholders_gen.go). Exact match: the retired values are lower-case
// already, and a case-folded near-miss is not the published string.
func IsRetiredPublicSecret(value string) bool {
	for _, r := range RetiredPublicSecrets {
		if value == r {
			return true
		}
	}
	return false
}
