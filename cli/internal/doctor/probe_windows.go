//go:build windows

package doctor

import "golang.org/x/sys/windows"

// soExclusiveAddrUse is winsock2.h's SO_EXCLUSIVEADDRUSE, ((int)(~SO_REUSEADDR)),
// which x/sys/windows does not name.
const soExclusiveAddrUse = ^windows.SO_REUSEADDR

// bindProbe is true when a bind of 0.0.0.0:port fails - the port is held
// (preflight.py:425-434).
//
// A raw socket through x/sys with SO_EXCLUSIVEADDRUSE, not net.Listen.
// §12 assumed a plain Windows listener is exclusive; TestBindProbeSees-
// ARealListener showed on the owner's box that it is not for the shape
// that matters: a wildcard bind SUCCEEDS while another socket holds
// 127.0.0.1:port, so net.Listen(":port") reported a false 'free' - the
// exact mistake the script's comment warns about. Winsock's rule (Using
// SO_EXCLUSIVEADDRUSE, learn.microsoft.com) is that a second bind carrying
// SO_EXCLUSIVEADDRUSE fails with WSAEADDRINUSE whenever ANY socket holds
// the port on either the wildcard or a specific address, which is the
// strict probe the Linux side gets by refusing SO_REUSEADDR. A socket we
// cannot even create is reported as in use: the check cannot prove the
// port free, and a spurious FAIL with its stated reason beats a spurious
// OK. Winsock is initialised by internal/poll's init (WSAStartup), which
// every Go binary links through package os.
func bindProbe(port int) bool {
	h, err := windows.Socket(windows.AF_INET, windows.SOCK_STREAM, windows.IPPROTO_TCP)
	if err != nil {
		return true
	}
	defer windows.Closesocket(h)
	if err := windows.SetsockoptInt(h, windows.SOL_SOCKET, soExclusiveAddrUse, 1); err != nil {
		return true
	}
	return windows.Bind(h, &windows.SockaddrInet4{Port: port}) != nil
}
