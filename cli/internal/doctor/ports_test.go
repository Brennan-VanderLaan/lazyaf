package doctor

import (
	"bytes"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// resolve is test_preflight.py's helper: resolvePort for one raw value,
// with its report captured.
func resolve(t *testing.T, raw *string) (int, bool, string) {
	t.Helper()
	values := map[string]string{}
	if raw != nil {
		values["LAZYAF_BACKEND_PORT"] = *raw
	}
	out := &bytes.Buffer{}
	r := &reporter{w: out}
	port, ok := resolvePort(r, values, "LAZYAF_BACKEND_PORT", 8000)
	return port, ok, out.String()
}

func str(s string) *string { return &s }

// tdd/unit/scripts/test_preflight.py::TestTheFormsAValidValueTakes::test_it_resolves
func TestPortForms(t *testing.T) {
	cases := []struct {
		name string
		raw  *string
		want int
	}{
		{"unset", nil, 8000},
		{"empty", str(""), 8000},
		{"blank", str("   "), 8000},
		{"bare 8000", str("8000"), 8000},
		{"bare 9001", str("9001"), 9001},
		// The shipped defaults. These are THE regression cases.
		{"loopback backend", str("127.0.0.1:8000"), 8000},
		{"loopback frontend", str("127.0.0.1:5173"), 5173},
		// Deliberately widening it is legal and must still parse.
		{"all interfaces", str("0.0.0.0:8000"), 8000},
		{"lan address", str("192.168.1.10:8080"), 8080},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			port, ok, out := resolve(t, tc.raw)
			if !ok || port != tc.want {
				t.Fatalf("resolvePort(%v) = %d, %v; want %d\n%s", tc.raw, port, ok, tc.want, out)
			}
			if out != "" {
				t.Fatalf("a valid value produced a report:\n%s", out)
			}
		})
	}
}

// tdd/unit/scripts/test_preflight.py::TestTheFormsAValidValueTakes::test_the_shipped_env_example_values_parse
//
// Read from .env.example itself, so the two cannot drift apart. A
// hardcoded "127.0.0.1:8000" here would keep passing after someone changed
// the template; this fails instead, which is the point.
func TestEnvExampleValuesParse(t *testing.T) {
	example := findUp(t, ".env.example")
	raw, err := os.ReadFile(example)
	if err != nil {
		t.Fatal(err)
	}
	shipped := map[string]string{}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		for _, name := range []string{"LAZYAF_BACKEND_PORT", "LAZYAF_FRONTEND_PORT"} {
			if strings.HasPrefix(line, name+"=") {
				shipped[name] = strings.TrimPrefix(line, name+"=")
			}
		}
	}
	if len(shipped) != 2 {
		t.Fatalf("the port variables left %s, or were commented out - doctor reads them, so this test needs updating deliberately (found %v)", example, shipped)
	}
	for name, value := range shipped {
		out := &bytes.Buffer{}
		port, ok := resolvePort(&reporter{w: out}, shipped, name, 8000)
		if !ok {
			t.Fatalf("%s=%q is what the project ships, and doctor refuses it - a new user's third command fails on a correct config:\n%s", name, value, out.String())
		}
		if port < 1 || port > 65535 {
			t.Fatalf("%s resolved to %d", name, port)
		}
	}
}

// findUp walks from the package directory to the repo root for name; a
// missing file is a Fatal naming the absolute path (never a Skip, R4).
func findUp(t *testing.T, name string) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	start := dir
	for {
		candidate := filepath.Join(dir, name)
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("%s not found walking up from %s - run the Go tests from a LazyAF checkout", name, start)
		}
		dir = parent
	}
}

// tdd/unit/scripts/test_preflight.py::TestTheFormsThatAreGenuinelyWrong
func TestPortRefusals(t *testing.T) {
	for _, raw := range []string{"banana", "127.0.0.1:nope", "127.0.0.1:", ":", "80 00"} {
		t.Run("wrong form "+raw, func(t *testing.T) {
			_, ok, out := resolve(t, str(raw))
			if ok {
				t.Fatalf("accepted %q", raw)
			}
			if !strings.Contains(out, "[FAIL]") || !strings.Contains(out, "is not a port") {
				t.Fatalf("report:\n%s", out)
			}
		})
	}
	for _, raw := range []string{"0", "65536", "127.0.0.1:0", "-1"} {
		t.Run("out of range "+raw, func(t *testing.T) {
			_, ok, out := resolve(t, str(raw))
			if ok {
				t.Fatalf("accepted %q", raw)
			}
			if !strings.Contains(out, "[FAIL]") || !strings.Contains(out, "1-65535") {
				t.Fatalf("report:\n%s", out)
			}
		})
	}
	t.Run("the refusal does not advise removing the host ip", func(t *testing.T) {
		// The remedy must not be "make it a bare port number". That was the
		// old advice, and following it republishes the API on every
		// interface. Whatever the message says, it must offer the
		// HOST_IP:PORT form and name the shipped loopback default.
		_, _, out := resolve(t, str("banana"))
		if !strings.Contains(out, "HOST_IP:PORT") && !strings.Contains(out, "0.0.0.0:") {
			t.Fatalf("the refusal does not tell the user the host-IP form is allowed:\n%s", out)
		}
		if !strings.Contains(out, "127.0.0.1") {
			t.Fatalf("the refusal does not name the shipped loopback default, so a user fixing it has no reason to keep the binding narrow:\n%s", out)
		}
	})
}

// §8.2: port_in_use = connect to 127.0.0.1 then bind, no SO_REUSEADDR
// (preflight.py:406-434). A real listener, on this OS.
func TestBindProbeSeesARealListener(t *testing.T) {
	// A port nothing holds: listen on :0, read the number, close.
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	_ = l.Close()
	if bindProbe(port) {
		t.Fatalf("bindProbe(%d) says in use with nothing bound", port)
	}
	if portInUse(port) {
		t.Fatalf("portInUse(%d) says in use with nothing bound", port)
	}

	// Now a real listener on the loopback interface only. The bind half
	// must see it: 0.0.0.0:port collides with 127.0.0.1:port when neither
	// socket asked for address reuse.
	l, err = net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		t.Fatalf("re-listen on %d: %v", port, err)
	}
	defer l.Close()
	if !bindProbe(port) {
		t.Fatalf("bindProbe(%d) says free while a listener holds it - the probe is reusing the address", port)
	}
	if !portInUse(port) {
		t.Fatalf("portInUse(%d) says free while a listener holds it", port)
	}
}
