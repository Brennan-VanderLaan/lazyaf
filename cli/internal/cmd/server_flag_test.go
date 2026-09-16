package cmd

import (
	"strings"
	"testing"
)

// TestExplicitEmptyServerIsRefused pins V4-2: `--server ""` used to fall
// through to $LAZYAF_SERVER and then to the built-in default, silently, because
// cobra cannot tell "passed as empty" from "not passed" by value alone. The
// root's PersistentPreRunE now records Flags().Changed("server"), and every
// client is built from that provenance (helpers.go serverSetting). An explicit
// empty URL is a refusal naming the two ways to set one, never a guess (R1).
func TestExplicitEmptyServerIsRefused(t *testing.T) {
	t.Setenv("LAZYAF_SERVER", "http://127.0.0.1:1") // must NOT be consulted

	out := run(t, nil, "--server", "", "list")
	if out.code == 0 {
		t.Fatalf("exit 0 for an explicit empty --server; stdout=%q stderr=%q", out.stdout, out.stderr)
	}
	for _, want := range []string{"--server is empty", "LAZYAF_SERVER=", "--server http://"} {
		if !strings.Contains(out.stderr, want) {
			t.Errorf("refusal lacks %q:\n%s", want, out.stderr)
		}
	}
	if strings.Contains(out.all(), "127.0.0.1:1") {
		t.Errorf("the environment URL was consulted despite an explicit empty flag:\n%s", out.all())
	}

	// doctor resolves its own URL for the provenance line; it must still
	// refuse an explicit empty flag before running a single check.
	d := run(t, nil, "--server", "", "doctor")
	if d.code == 0 || !strings.Contains(d.stderr, "--server is empty") {
		t.Errorf("doctor with an explicit empty --server: code=%d stderr=%q", d.code, d.stderr)
	}

	// --version must still never touch the flag or the network.
	v := run(t, nil, "--server", "", "--version", "--short")
	if v.code != 0 || !strings.HasPrefix(strings.TrimSpace(v.stdout), "dev") && !strings.Contains(v.stdout, ".") {
		t.Fatalf("--version with an empty --server: code=%d stdout=%q stderr=%q", v.code, v.stdout, v.stderr)
	}
}
