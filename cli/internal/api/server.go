// Package api is THE one HTTP client of the binary (R3).
//
// Every command routes through Client.Request so that "the backend said no"
// reads the same everywhere. The Python CLI learned this the hard way
// (cli/lazyaf/cli.py:250-262: three commands printed the status code and
// dropped the body, two dumped the raw envelope, only the newest quoted the
// server). TestOnlyAPIImportsNetHTTP keeps it that way with go/parser: no
// other non-test package under cli/internal may import net/http.
package api

import (
	"fmt"
	"os"
	"strings"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// Where the backend URL comes from, in precedence order (§5):
// --server > $LAZYAF_SERVER > the built-in default.
const (
	DefaultServer = "http://localhost:8000"
	ServerEnvVar  = "LAZYAF_SERVER"
)

// ServerSetting is the --server flag with its provenance.
//
// Explicit is true when the flag was passed AT ALL, so `--server ""` is
// distinguishable from an unset flag. The two must differ: the Python twin
// takes `server if server is not None` (cli.py:185), so an explicit empty
// flag is refused as "--server is empty" while an unset one falls through
// to $LAZYAF_SERVER. A bare string cannot carry that bit, which is why
// callers that can see it (cobra's Flags().Changed("server")) build the
// setting themselves rather than passing the value through New.
type ServerSetting struct {
	Value    string
	Explicit bool
}

// FlagSetting is the setting a plain flag value implies: explicit when
// non-empty. It cannot see an explicit `--server ""`; that is the one case
// the string-taking entry points (New, ResolveServerURL, DescribeServer)
// get wrong, and the reason ServerSetting exists.
func FlagSetting(value string) ServerSetting {
	return ServerSetting{Value: value, Explicit: value != ""}
}

// serverSource names where the URL we are about to use came from.
//
// Half of "could not connect" reports are really "connected to the wrong
// thing": a stale $LAZYAF_SERVER in one shell, the default in another. The
// URL alone does not settle that; the URL plus its provenance does
// (cli.py:153-166).
func serverSource(s ServerSetting) string {
	if s.Explicit {
		return "--server"
	}
	if os.Getenv(ServerEnvVar) != "" {
		return "$" + ServerEnvVar
	}
	return fmt.Sprintf("the built-in default (%s)", DefaultServer)
}

// rawServerURL is the setting before validation: the explicit flag, else
// the environment, else the default. An explicit flag set to "" is "", and
// an env var set to "" is "" - an empty setting is refused by ResolveServer,
// not papered over.
func rawServerURL(s ServerSetting) string {
	if s.Explicit {
		return s.Value
	}
	if v, ok := os.LookupEnv(ServerEnvVar); ok {
		return v
	}
	return DefaultServer
}

// DescribeServer is DescribeSetting for a plain flag value (see FlagSetting
// for what a plain value cannot express).
func DescribeServer(explicit string) string {
	return DescribeSetting(FlagSetting(explicit))
}

// DescribeSetting is one line naming the backend in use and how to change it.
func DescribeSetting(s ServerSetting) string {
	return fmt.Sprintf("LazyAF backend: %s (from %s)\nChange it with --server <url> or %s=<url>.",
		rawServerURL(s), serverSource(s), ServerEnvVar)
}

// ResolveServerURL is ResolveServer for a plain flag value (see FlagSetting
// for what a plain value cannot express).
func ResolveServerURL(explicit string) (string, error) {
	return ResolveServer(FlagSetting(explicit))
}

// ResolveServer is the backend base URL, validated and normalised, or a
// refusal.
//
// A missing scheme is REFUSED, not guessed - the same rule
// terminal.TerminalURL applies, for the same reason (R3: one rule for one
// question). Guessing http:// is how a request that should have been
// encrypted goes out in the clear. The refusal shows the fix with the
// scheme filled in.
func ResolveServer(s ServerSetting) (string, error) {
	source := serverSource(s)
	url := strings.TrimRight(strings.TrimSpace(rawServerURL(s)), "/")

	if url == "" {
		return "", ui.Fail(
			fmt.Sprintf("no LazyAF backend URL: %s is empty", source),
			ui.WithRemedy(fmt.Sprintf("Set one:\n    %s=%s\nor pass it per command:\n    lazyaf list --server %s",
				ServerEnvVar, DefaultServer, DefaultServer)),
		)
	}
	if !strings.HasPrefix(url, "http://") && !strings.HasPrefix(url, "https://") {
		guess := url
		if i := strings.Index(url, "://"); i >= 0 {
			guess = url[i+3:]
		}
		return "", ui.Fail(
			fmt.Sprintf("the LazyAF backend URL has no http:// or https:// scheme: %s", url),
			ui.WithRemedy(fmt.Sprintf(
				"Read from %s. The scheme is required rather than guessed - guessing "+
					"http:// is how a request that should have been encrypted goes out "+
					"in the clear.\n\n    %s=http://%s\n    lazyaf list --server http://%s",
				source, ServerEnvVar, guess, guess)),
		)
	}
	return url, nil
}
