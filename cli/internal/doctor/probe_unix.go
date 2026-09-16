//go:build !windows

package doctor

import "golang.org/x/sys/unix"

// bindProbe is true when a bind of 0.0.0.0:port fails - the port is held
// (preflight.py:425-434).
//
// A raw socket through x/sys, not net.Listen: Go's net package sets
// SO_REUSEADDR on every Linux listener (net/sockopt_linux.go
// setDefaultListenerSockopts), which is precisely the option the script
// refuses to set - it would let this probe bind a port someone else
// already owns and report a false 'free'. A socket we cannot even create
// is reported as in use: the check cannot prove the port free, and a
// spurious FAIL with its stated reason beats a spurious OK.
func bindProbe(port int) bool {
	fd, err := unix.Socket(unix.AF_INET, unix.SOCK_STREAM, 0)
	if err != nil {
		return true
	}
	unix.CloseOnExec(fd)
	defer unix.Close(fd)
	return unix.Bind(fd, &unix.SockaddrInet4{Port: port}) != nil
}
