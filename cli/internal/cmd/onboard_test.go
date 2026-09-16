package cmd

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// `lazyaf init` and `lazyaf doctor` wiring (upcoming/go-cli.md §5, §8).
//
// The properties of the two commands live in internal/initcmd and
// internal/doctor, each with its own suite. What this file proves is that
// the binary REACHES them: both were complete and green while nothing in
// the cobra tree registered them, so `lazyaf init` was `unknown command`
// and the release smoke (release.yml, §9.1) was red on every run
// (verifier findings V3-2 / V4-1). The steps below are the smoke's, run
// through the same NewRootWith + Run path main.go uses.

// secretRun is the shape of a generated value (64 URL-safe chars, §8.1),
// which must never appear on either stream.
var secretRun = regexp.MustCompile(`[A-Za-z0-9_-]{64}`)

func TestInitCommand(t *testing.T) {
	t.Run("check on a nonexistent env file exits 1 printing MISSING and creates nothing", func(t *testing.T) {
		// release.yml smoke: `init --check --env-file $RUNNER_TEMP/nope`.
		nope := filepath.Join(t.TempDir(), "nope")
		o := run(t, nil, "init", "--check", "--env-file", nope)
		if o.code != 1 {
			t.Fatalf("exit %d, want 1:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "MISSING")
		if _, err := os.Stat(nope); err == nil {
			t.Fatalf("--check created %s; it must change nothing", nope)
		}
	})
	t.Run("an unmarked directory is refused naming the marker", func(t *testing.T) {
		o := run(t, nil, "init", "--dir", t.TempDir())
		if o.code != 1 {
			t.Fatalf("exit %d, want 1:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, ".env.example", "--env-file")
	})
	t.Run("a marked directory gets both secrets and no value is printed", func(t *testing.T) {
		// §13.3 line 6: `lazyaf init` exits 0 in a marked directory and
		// prints no value - asserted here on the real streams, not inside
		// the package.
		// Not t.TempDir(): it bakes this subtest's name into the path, init
		// prints the path, and a 60+ char path segment matches secretRun.
		dir, err := os.MkdirTemp("", "lazyaf-init-")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = os.RemoveAll(dir) })
		template := "# example\nLAZYAF_STEP_AUTH_SECRET=\nLAZYAF_RUNNER_AUTH_SECRET=\n"
		if err := os.WriteFile(filepath.Join(dir, ".env.example"), []byte(template), 0o644); err != nil {
			t.Fatal(err)
		}
		o := run(t, nil, "init", "--dir", dir)
		if o.code != 0 {
			t.Fatalf("exit %d, want 0:\n%s", o.code, o.all())
		}
		raw, err := os.ReadFile(filepath.Join(dir, ".env"))
		if err != nil {
			t.Fatalf(".env was not written: %v", err)
		}
		for _, key := range []string{"LAZYAF_STEP_AUTH_SECRET", "LAZYAF_RUNNER_AUTH_SECRET"} {
			var value string
			for _, line := range strings.Split(string(raw), "\n") {
				if strings.HasPrefix(line, key+"=") && len(line) > len(key)+1 {
					value = strings.TrimSpace(strings.TrimPrefix(line, key+"="))
				}
			}
			if len(value) < 43 {
				t.Fatalf("%s is %d chars in .env, want >= 43:\n%s", key, len(value), raw)
			}
			if strings.Contains(o.all(), value) {
				t.Fatalf("the generated %s was printed", key)
			}
		}
		if m := secretRun.FindString(o.all()); m != "" {
			t.Fatalf("a secret-shaped run was printed: %s", m)
		}
		// Idempotent: the second run changes nothing and still exits 0.
		if o2 := run(t, nil, "init", "--dir", dir); o2.code != 0 {
			t.Fatalf("second run exit %d:\n%s", o2.code, o2.all())
		}
		if again, _ := os.ReadFile(filepath.Join(dir, ".env")); string(again) != string(raw) {
			t.Fatal("the second run rewrote .env")
		}
		if o3 := run(t, nil, "init", "--check", "--dir", dir); o3.code != 0 {
			t.Fatalf("--check after init exit %d:\n%s", o3.code, o3.all())
		}
	})
	t.Run("help exits 0 and names the check flag", func(t *testing.T) {
		o := run(t, nil, "init", "--help")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "--check", "--env-file", "lazyaf init")
	})
}

func TestDoctorCommand(t *testing.T) {
	t.Run("help exits 0 and documents the server check", func(t *testing.T) {
		// release.yml smoke: `doctor --help`.
		o := run(t, nil, "doctor", "--help")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "--dev", "--offline", "--dir", "--server", "lazyaf doctor --server")
		if strings.Contains(o.stdout, "debug-trace") {
			t.Fatal("--debug-trace is for bug reports and must stay hidden from --help")
		}
	})
	t.Run("server is the root's persistent flag, not a second one", func(t *testing.T) {
		// §5: doctor's "new --server URL" is cmd.ServerFlag, so there is
		// exactly one resolution rule (flag > $LAZYAF_SERVER > default).
		root := NewRootWith(fakeDeps(t))
		c, _, err := root.Find([]string{"doctor"})
		if err != nil || c.Name() != "doctor" {
			t.Fatalf("doctor is not in the tree: %v", err)
		}
		if c.LocalFlags().Lookup("server") != nil {
			t.Fatal("doctor declares its own --server; it must inherit the root's")
		}
		if c.InheritedFlags().Lookup("server") == nil {
			t.Fatal("doctor does not inherit the persistent --server")
		}
		var visible []string
		for _, name := range []string{"dev", "offline", "dir", "debug-trace"} {
			if c.Flags().Lookup(name) == nil {
				t.Fatalf("doctor lacks --%s", name)
			}
			if !c.Flags().Lookup(name).Hidden {
				visible = append(visible, name)
			}
		}
		if strings.Join(visible, ",") != "dev,offline,dir" {
			t.Fatalf("visible doctor flags = %v, want the §5 three", visible)
		}
		// Both parse positions work, as every other command's --server does.
		for _, args := range [][]string{
			{"doctor", "--server", "http://127.0.0.1:1", "--help"},
			{"--server", "http://127.0.0.1:1", "doctor", "--help"},
		} {
			if o := run(t, nil, args...); o.code != 0 {
				t.Fatalf("%v: exit %d:\n%s", args, o.code, o.all())
			}
		}
	})
	t.Run("a positional is a usage error carrying the example", func(t *testing.T) {
		o := run(t, nil, "doctor", "extra")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, "unexpected extra argument", "lazyaf doctor --dev")
	})
}
