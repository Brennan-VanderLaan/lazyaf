package doctor

import (
	"fmt"
	"net"
	"strconv"
	"strings"
	"time"
)

// connectProbeTimeout is preflight.py:416's settimeout(0.5).
const connectProbeTimeout = 500 * time.Millisecond

// resolvePort reads a port from .env, reporting a bad value instead of
// crashing (preflight.py:437-475).
//
// The value is interpolated straight into a docker compose port mapping,
// so it takes the full `[HOST_IP:]PORT` form docker accepts - and since the
// Rung 0 hardening it DEFAULTS to 127.0.0.1:8000 / 127.0.0.1:5173 in
// .env.example. A bare int(raw) therefore failed on a correctly configured
// install and told the user to "set it to a port number", whose only
// effect would have been to republish an unauthenticated API on 0.0.0.0. A
// preflight that fails a good config and talks the user into a worse one is
// worse than no preflight (R1); test_preflight.py exists for that
// regression and doctor.TestPortRefusals keeps the advice honest.
//
// Only the PORT half is returned: the free-port probe connects to
// 127.0.0.1 regardless.
func resolvePort(r *reporter, values map[string]string, name string, def int) (int, bool) {
	raw := strings.TrimSpace(values[name])
	if raw == "" {
		return def, true
	}
	candidate := raw
	if i := strings.LastIndex(raw, ":"); i >= 0 {
		candidate = raw[i+1:]
	}
	port, err := strconv.Atoi(candidate)
	if err != nil {
		r.report(statusFail,
			fmt.Sprintf("%s is not a port (%q)", name, raw),
			fmt.Sprintf("Use PORT or HOST_IP:PORT - e.g. %d or 0.0.0.0:%d.", def, def),
			fmt.Sprintf("Remove it from .env to use the shipped default, 127.0.0.1:%d.", def),
		)
		return 0, false
	}
	if port < 1 || port > 65535 {
		r.report(statusFail,
			fmt.Sprintf("%s is not a valid port (%d)", name, port),
			fmt.Sprintf("Ports run 1-65535. Remove it from .env to use %d.", def),
		)
		return 0, false
	}
	return port, true
}

// checkPorts: the published host ports are free, or already ours
// (preflight.py:478-505).
func checkPorts(r *reporter, values map[string]string, opts Options) {
	for _, spec := range []struct {
		name string
		def  int
		what string
	}{
		{"LAZYAF_BACKEND_PORT", 8000, "backend API"},
		{"LAZYAF_FRONTEND_PORT", 5173, "web UI"},
	} {
		port, ok := resolvePort(r, values, spec.name, spec.def)
		if !ok {
			continue // resolvePort already reported why
		}
		if !portInUse(port) {
			r.report(statusOK, fmt.Sprintf("Port %d is free (for the %s)", port, spec.what))
			continue
		}
		if owner := portOwnerHint(opts, port); owner != "" {
			r.report(statusWarn,
				fmt.Sprintf("Port %d is already used by a container: %s", port, owner),
				"If that is a LazyAF stack you already started, nothing to do.",
				fmt.Sprintf("Otherwise stop it, or set %s in .env to a free port.", spec.name),
			)
		} else {
			r.report(statusFail,
				fmt.Sprintf("Port %d is in use (needed for the %s)", port, spec.what),
				fmt.Sprintf("Stop whatever is listening, or set %s in .env to a free port.", spec.name),
			)
		}
	}
}

// portOwnerHint is a best-effort hint about who holds a port. Never fails
// the check (preflight.py:394-403).
func portOwnerHint(opts Options, port int) string {
	code, out := opts.Exec.Run(15*time.Second, "docker", "ps",
		"--filter", fmt.Sprintf("publish=%d", port), "--format", "{{.Names}} ({{.Image}})")
	if code == 0 && out != "" {
		return firstLine(out)
	}
	return ""
}

// portInUse is true when something already holds this port on localhost
// (preflight.py:406-434).
//
// Two probes, because neither alone is reliable. A CONNECT proves someone
// is listening and is the one that catches Docker's published ports on
// Windows, where a fresh bind to 127.0.0.1 can succeed over the engine's
// existing 0.0.0.0 bind and report a false 'free'. A BIND catches the
// rest: a socket that is bound but not accepting, or bound on another
// interface. The bind is deliberately WITHOUT address reuse (bindProbe,
// per platform): a raw x/sys socket with no SO_REUSEADDR on unix, and with
// SO_EXCLUSIVEADDRUSE on Windows. net.Listen would report a false 'free'
// on both: Go sets SO_REUSEADDR on every Linux listener, and a plain
// Windows wildcard bind succeeds over another socket's 127.0.0.1 bind.
func portInUse(port int) bool {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), connectProbeTimeout)
	if err == nil {
		_ = conn.Close()
		return true
	}
	return bindProbe(port)
}
