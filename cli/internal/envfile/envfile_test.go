package envfile

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// urlSafe is the alphabet of token_urlsafe and base64.RawURLEncoding.
var urlSafe = regexp.MustCompile(`^[A-Za-z0-9_\-]+$`)

func noEnv(string) string { return "" }

// valueOf runs Plan over lines and reads the value it would write for key
// back through the real writer, so a test sees exactly the bytes a .env
// would hold and never touches Secret's internals.
func planned(t *testing.T, lines []string, getenv func(string) string) (map[string]string, []Action, string) {
	t.Helper()
	doc, actions, err := Plan(lines, getenv)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), ".env")
	if err := AtomicWrite(path, doc, "\n"); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return ReadValues(SplitLines(string(raw))), actions, string(raw)
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_generates_both_secrets_with_real_entropy
func TestGenerateSecretShape(t *testing.T) {
	values, _, _ := planned(t, nil, noEnv)
	for _, m := range ManagedSecrets {
		v := values[m.Key]
		if len(v) != 64 {
			t.Fatalf("%s is %d chars, want 64 (48 bytes URL-safe base64, no padding)", m.Key, len(v))
		}
		if len(v) < 43 {
			t.Fatalf("%s is too short to be generated", m.Key)
		}
		if !urlSafe.MatchString(v) {
			t.Fatalf("%s is not URL-safe: %q", m.Key, v)
		}
	}
	if values[ManagedSecrets[0].Key] == values[ManagedSecrets[1].Key] {
		t.Fatal("one value used twice is one secret")
	}
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_two_installations_do_not_share_a_secret
func TestTwoRunsDiffer(t *testing.T) {
	first, _, _ := planned(t, nil, noEnv)
	second, _, _ := planned(t, nil, noEnv)
	for _, m := range ManagedSecrets {
		if first[m.Key] == second[m.Key] {
			t.Fatalf("%s: per-installation, not per-release - otherwise it is a default again", m.Key)
		}
	}
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_an_existing_value_is_never_overwritten
func TestKeptIsKept(t *testing.T) {
	values, actions, _ := planned(t, []string{"LAZYAF_STEP_AUTH_SECRET=my-own-carefully-chosen-value"}, noEnv)
	if values["LAZYAF_STEP_AUTH_SECRET"] != "my-own-carefully-chosen-value" {
		t.Fatalf("existing value overwritten: %q", values["LAZYAF_STEP_AUTH_SECRET"])
	}
	if len(values["LAZYAF_RUNNER_AUTH_SECRET"]) < 43 {
		t.Fatal("the missing one is still generated")
	}
	if actions[0] != (Action{"LAZYAF_STEP_AUTH_SECRET", Kept}) || actions[1] != (Action{"LAZYAF_RUNNER_AUTH_SECRET", Generated}) {
		t.Fatalf("actions = %v", actions)
	}
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_unrelated_keys_and_comments_survive
func TestUnrelatedLinesSurvive(t *testing.T) {
	original := []string{
		"# my notes",
		// Deliberately NOT key-shaped: .github/scripts/scan_repo_secrets.py
		// fails the build on any `sk-ant-` string that is not allowlisted.
		"ANTHROPIC_API_KEY=redacted-fixture-value",
		"CLAUDE_RUNNERS=4",
		"",
		"# LAZYAF_BACKEND_PORT=",
	}
	_, _, text := planned(t, original, noEnv)
	for _, want := range original {
		if !strings.Contains(text, want+"\n") {
			t.Fatalf("%q did not survive:\n%s", want, text)
		}
	}
	if !strings.HasPrefix(text, strings.Join(original, "\n")+"\n\n"+GeneratedHeader) {
		t.Fatalf("the operator's lines must come first, then one blank, then the block:\n%s", text)
	}
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_an_empty_assignment_is_filled_in_place
func TestEmptyAssignmentFilledInPlace(t *testing.T) {
	t.Run("filled where it stands with no shadowing duplicate", func(t *testing.T) {
		values, _, text := planned(t, []string{"LAZYAF_STEP_AUTH_SECRET=", "CLAUDE_RUNNERS=4"}, noEnv)
		lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
		if !strings.HasPrefix(lines[0], "LAZYAF_STEP_AUTH_SECRET=") {
			t.Fatalf("line 0 = %q", lines[0])
		}
		if len(values["LAZYAF_STEP_AUTH_SECRET"]) < 43 {
			t.Fatal("not filled")
		}
		n := 0
		for _, l := range lines {
			if strings.HasPrefix(l, "LAZYAF_STEP_AUTH_SECRET=") {
				n++
			}
		}
		if n != 1 {
			t.Fatalf("%d assignments for the key, want exactly 1:\n%s", n, text)
		}
	})
	t.Run("the last active assignment is the one replaced", func(t *testing.T) {
		// dotenv honours the last one; replacing the first would leave the
		// empty one shadowing the new value.
		_, _, text := planned(t, []string{"LAZYAF_STEP_AUTH_SECRET=changeme", "X=1", "LAZYAF_STEP_AUTH_SECRET="}, noEnv)
		lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
		if lines[0] != "LAZYAF_STEP_AUTH_SECRET=changeme" {
			t.Fatalf("the first assignment was touched: %q", lines[0])
		}
		if len(lines[2]) < len("LAZYAF_STEP_AUTH_SECRET=")+43 {
			t.Fatalf("the last assignment was not filled: %q", lines[2])
		}
	})
	t.Run("quotes and export are assignments too", func(t *testing.T) {
		for _, line := range []string{`export LAZYAF_STEP_AUTH_SECRET=""`, `LAZYAF_STEP_AUTH_SECRET='changeme'`, "  LAZYAF_STEP_AUTH_SECRET =  "} {
			_, actions, text := planned(t, []string{line}, noEnv)
			if actions[0].Kind != Generated {
				t.Fatalf("%q: action %v, want generated", line, actions[0])
			}
			if strings.Count(text, "LAZYAF_STEP_AUTH_SECRET") != 1 {
				t.Fatalf("%q: replaced in place, so exactly one occurrence expected:\n%s", line, text)
			}
			if strings.Contains(text, line+"\n") {
				t.Fatalf("%q was left as it was:\n%s", line, text)
			}
		}
	})
	t.Run("a commented assignment is documentation not an assignment", func(t *testing.T) {
		_, _, text := planned(t, []string{"# LAZYAF_STEP_AUTH_SECRET="}, noEnv)
		if !strings.Contains(text, "# LAZYAF_STEP_AUTH_SECRET=\n") {
			t.Fatalf("the comment was edited:\n%s", text)
		}
		if !strings.Contains(text, GeneratedHeader) {
			t.Fatalf("a fresh block should have been appended:\n%s", text)
		}
	})
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_a_retired_default_or_placeholder_is_replaced
func TestPlaceholderReplaced(t *testing.T) {
	stale := []string{"changeme", "TODO", "xxxxxxxx", "XXXX", "<generate>", " "}
	stale = append(stale, RetiredPublicSecrets...)
	stale = append(stale, PlaceholderSecrets...)
	// The quoted-empty form is a placeholder only once SplitAssignment has
	// stripped the quotes, which is why it is in the plan half alone.
	for _, s := range append(stale, "\"\"") {
		t.Run(s, func(t *testing.T) {
			if s != "\"\"" && !IsPlaceholder(s) {
				t.Fatalf("IsPlaceholder(%q) = false", s)
			}
			values, _, _ := planned(t, []string{"LAZYAF_STEP_AUTH_SECRET=" + s}, noEnv)
			got := values["LAZYAF_STEP_AUTH_SECRET"]
			if got == strings.TrimSpace(s) || len(got) < 43 {
				t.Fatalf("an inherited .env must not keep %q; got %q", s, got)
			}
		})
	}
	t.Run("a real value is not a placeholder", func(t *testing.T) {
		for _, real := range []string{"my-own-carefully-chosen-value", "xyzzy", "Secretive-9f2a", "nonexistent"} {
			if IsPlaceholder(real) {
				t.Fatalf("IsPlaceholder(%q) = true", real)
			}
		}
	})
	t.Run("the retired defaults are matched exactly not case folded", func(t *testing.T) {
		if IsRetiredPublicSecret(strings.ToUpper(RetiredPublicSecrets[0])) {
			t.Fatal("an upper-cased near-miss is not the published string")
		}
	})
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_a_file_pointer_is_respected_instead_of_generating
func TestFilePointerDelegates(t *testing.T) {
	t.Run("pointer in the file", func(t *testing.T) {
		values, actions, _ := planned(t, []string{"LAZYAF_STEP_AUTH_SECRET_FILE=/run/secrets/step"}, noEnv)
		if _, written := values["LAZYAF_STEP_AUTH_SECRET"]; written {
			t.Fatal("a value was written next to a _FILE pointer; it would be silently shadowed")
		}
		if actions[0] != (Action{"LAZYAF_STEP_AUTH_SECRET", Delegated}) {
			t.Fatalf("actions = %v", actions)
		}
	})
	t.Run("pointer in the ambient environment", func(t *testing.T) {
		env := func(k string) string {
			if k == "LAZYAF_RUNNER_AUTH_SECRET_FILE" {
				return "/run/secrets/runner"
			}
			return ""
		}
		values, actions, _ := planned(t, nil, env)
		if _, written := values["LAZYAF_RUNNER_AUTH_SECRET"]; written {
			t.Fatal("a value was written despite the exported _FILE")
		}
		if actions[1] != (Action{"LAZYAF_RUNNER_AUTH_SECRET", Delegated}) {
			t.Fatalf("actions = %v", actions)
		}
	})
	t.Run("a blank pointer delegates nothing", func(t *testing.T) {
		_, actions, _ := planned(t, []string{"LAZYAF_STEP_AUTH_SECRET_FILE=   "}, noEnv)
		if actions[0].Kind != Generated {
			t.Fatalf("actions = %v", actions)
		}
	})
}

// §8.1: the Secret type is the structural half of "never prints a secret".
func TestSecretRedactsUnderEveryVerb(t *testing.T) {
	const sentinel = "SENTINEL-VALUE-THAT-MUST-NOT-APPEAR-0123456789"
	s := NewSecret(sentinel)
	type holder struct {
		Exported Secret
		Ptr      *Secret
		hidden   Secret
	}
	h := holder{Exported: s, Ptr: &s, hidden: s}
	cases := map[string]string{
		"%v":            fmt.Sprintf("%v", s),
		"%s":            fmt.Sprintf("%s", s),
		"%+v":           fmt.Sprintf("%+v", s),
		"%#v":           fmt.Sprintf("%#v", s),
		"%q":            fmt.Sprintf("%q", s),
		"%x":            fmt.Sprintf("%x", s),
		"%d":            fmt.Sprintf("%d", s),
		"%v ptr":        fmt.Sprintf("%v", &s),
		"Sprint":        fmt.Sprint(s),
		"Sprintln":      fmt.Sprintln(s),
		"String":        s.String(),
		"GoString":      s.GoString(),
		"%v holder":     fmt.Sprintf("%v", h),
		"%+v holder":    fmt.Sprintf("%+v", h),
		"%#v holder":    fmt.Sprintf("%#v", h),
		"%v holder ptr": fmt.Sprintf("%v", &h),
	}
	if b, err := json.Marshal(s); err != nil {
		t.Fatal(err)
	} else {
		cases["json"] = string(b)
	}
	if b, err := json.Marshal(h); err != nil {
		t.Fatal(err)
	} else {
		cases["json holder"] = string(b)
	}
	if b, err := s.MarshalText(); err != nil {
		t.Fatal(err)
	} else {
		cases["MarshalText"] = string(b)
	}
	for name, got := range cases {
		if strings.Contains(got, sentinel) {
			t.Errorf("%s printed the value: %s", name, got)
		}
		// encoding/json writes < as <; "redacted" is the word to look for.
		if !strings.Contains(got, "%!") && !strings.Contains(got, "redacted") && !strings.Contains(got, "{") {
			t.Errorf("%s = %q: neither redacted nor a struct shape", name, got)
		}
	}
	// hidden is reached through an unexported field, where fmt skips the
	// Formatter - the vault design is what keeps that path dry too.
	if strings.Contains(fmt.Sprintf("%+v", h), sentinel) {
		t.Fatal("the unexported-field path leaked the value")
	}
	// The one way out.
	var buf bytes.Buffer
	if _, err := s.WriteTo(&buf); err != nil || buf.String() != sentinel {
		t.Fatalf("WriteTo = %q, %v", buf.String(), err)
	}
	if _, err := (Secret{}).WriteTo(&buf); err == nil {
		t.Fatal("a zero Secret must refuse to write")
	}
}

// tdd/unit/scripts/test_bootstrap_secrets.py::test_a_stale_lock_does_not_wedge_the_next_run_forever
func TestStaleLockIsBroken(t *testing.T) {
	t.Run("a stale lock is unlinked and taken", func(t *testing.T) {
		target := filepath.Join(t.TempDir(), ".env")
		lock := LockPath(target)
		if err := os.WriteFile(lock, []byte("99999"), 0o600); err != nil {
			t.Fatal(err)
		}
		old := time.Now().Add(-LockStale - 10*time.Second)
		if err := os.Chtimes(lock, old, old); err != nil {
			t.Fatal(err)
		}
		start := time.Now()
		l, err := Lock(target, 2*time.Second)
		if err != nil {
			t.Fatalf("Lock: %v", err)
		}
		if took := time.Since(start); took > time.Second {
			t.Fatalf("breaking a stale lock took %s; it should not have waited", took)
		}
		l.Unlock()
		if _, err := os.Stat(lock); !os.IsNotExist(err) {
			t.Fatalf("lock left behind: %v", err)
		}
	})
	t.Run("a fresh lock is waited on then refused by name", func(t *testing.T) {
		target := filepath.Join(t.TempDir(), ".env")
		held, err := Lock(target, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		defer held.Unlock()
		start := time.Now()
		_, err = Lock(target, 300*time.Millisecond)
		var timeout *LockTimeoutError
		if err == nil || !asLockTimeout(err, &timeout) {
			t.Fatalf("Lock on a held lock = %v, want *LockTimeoutError", err)
		}
		if time.Since(start) < 300*time.Millisecond {
			t.Fatal("gave up before the timeout")
		}
		if !strings.Contains(err.Error(), LockPath(target)) {
			t.Fatalf("the refusal must name the file: %v", err)
		}
	})
}

func asLockTimeout(err error, target **LockTimeoutError) bool {
	e, ok := err.(*LockTimeoutError)
	if ok {
		*target = e
	}
	return ok
}

// The one parser (§8.2), and the things it must agree with itself about.
func TestReadValues(t *testing.T) {
	lines := []string{
		"# comment",
		"",
		"PLAIN=value",
		"export EXPORTED=ex",
		`QUOTED="q v"`,
		"SINGLE='s v'",
		"MISMATCH=\"x'",
		"  SPACED = padded  ",
		"NOEQUALS",
		"=novalue",
		"LAST=1",
		"LAST=2",
		"URL=http://h:1/?a=b=c",
	}
	got := ReadValues(lines)
	want := map[string]string{
		"PLAIN": "value", "EXPORTED": "ex", "QUOTED": "q v", "SINGLE": "s v",
		"MISMATCH": "\"x'", "SPACED": "padded", "LAST": "2", "URL": "http://h:1/?a=b=c",
	}
	if len(got) != len(want) {
		t.Fatalf("ReadValues = %v, want %v", got, want)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %q, want %q", k, got[k], v)
		}
	}
}

// bootstrap_secrets.py:193-199: CRLF detected and preserved.
func TestCRLFPreserved(t *testing.T) {
	if got := DetectNewline("a\r\nb\r\n"); got != "\r\n" {
		t.Fatalf("DetectNewline(CRLF) = %q", got)
	}
	if got := DetectNewline("a\nb\n"); got != "\n" {
		t.Fatalf("DetectNewline(LF) = %q", got)
	}
	if got := DetectNewline("no newline"); got != platformNewline {
		t.Fatalf("DetectNewline(none) = %q, want the platform's", got)
	}
	path := filepath.Join(t.TempDir(), ".env")
	if err := os.WriteFile(path, []byte("A=1\r\nLAZYAF_STEP_AUTH_SECRET=\r\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	lines, newline, err := Load(path)
	if err != nil || newline != "\r\n" || len(lines) != 2 || lines[0] != "A=1" {
		t.Fatalf("Load = %q, %q, %v", lines, newline, err)
	}
	doc, _, err := Plan(lines, noEnv)
	if err != nil {
		t.Fatal(err)
	}
	if err := AtomicWrite(path, doc, newline); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	if strings.Count(string(raw), "\r\n") != strings.Count(string(raw), "\n") {
		t.Fatalf("a bare LF crept into a CRLF file:\n%q", raw)
	}
	if !strings.HasPrefix(string(raw), "A=1\r\nLAZYAF_STEP_AUTH_SECRET=") {
		t.Fatalf("rewritten as %q", raw)
	}
}

func TestSplitLinesMatchesPython(t *testing.T) {
	for raw, want := range map[string][]string{
		"":             nil,
		"a":            {"a"},
		"a\n":          {"a"},
		"a\nb":         {"a", "b"},
		"a\r\nb\r\n":   {"a", "b"},
		"a\n\nb\n":     {"a", "", "b"},
		"\n":           {""},
		"a\r\n\r\nb":   {"a", "", "b"},
		"trailing\n\n": {"trailing", ""},
	} {
		got := SplitLines(raw)
		if strings.Join(got, "|") != strings.Join(want, "|") || (got == nil) != (want == nil) {
			t.Errorf("SplitLines(%q) = %q, want %q", raw, got, want)
		}
	}
}

// bootstrap_secrets.py:338-360: sibling .tmp-<pid>, O_EXCL, replaced; no
// leftover on either path.
func TestAtomicWrite(t *testing.T) {
	t.Run("replaces and leaves no temp file", func(t *testing.T) {
		dir := t.TempDir()
		path := filepath.Join(dir, ".env")
		if err := os.WriteFile(path, []byte("OLD=1\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := AtomicWrite(path, []Line{Verbatim("NEW=2")}, "\n"); err != nil {
			t.Fatal(err)
		}
		raw, _ := os.ReadFile(path)
		if string(raw) != "NEW=2\n" {
			t.Fatalf("file = %q", raw)
		}
		entries, _ := os.ReadDir(dir)
		if len(entries) != 1 {
			t.Fatalf("leftovers: %v", entries)
		}
	})
	t.Run("a temp file that already exists is a refusal not a clobber", func(t *testing.T) {
		dir := t.TempDir()
		path := filepath.Join(dir, ".env")
		tmp := path + ".tmp-" + fmt.Sprint(os.Getpid())
		if err := os.WriteFile(tmp, []byte("someone else's"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := AtomicWrite(path, []Line{Verbatim("X=1")}, "\n"); err == nil {
			t.Fatal("O_EXCL did not refuse an existing temp file")
		}
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatal("the target was written despite the refusal")
		}
	})
	t.Run("the secret is written and the rest verbatim", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), ".env")
		s := NewSecret("the-bytes")
		if err := AtomicWrite(path, []Line{Verbatim("# c"), Assignment("K", s)}, "\n"); err != nil {
			t.Fatal(err)
		}
		raw, _ := os.ReadFile(path)
		if string(raw) != "# c\nK=the-bytes\n" {
			t.Fatalf("file = %q", raw)
		}
	})
}
