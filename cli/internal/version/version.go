// Package version is the one place the binary learns what it is.
//
// upcoming/go-cli.md §4: the tag is the version, the binary learns it through
// `-ldflags -X` from scripts/build_cli.sh, and nothing in the tree carries a
// bumpable CLI version. A plain `go build` leaves Version at "dev", and
// Describe then refuses to print a bare semver - a source build N commits
// past a release must never claim to be that release ("dev must say dev").
package version

import (
	"fmt"
	"runtime"
	"runtime/debug"
	"strings"
)

// Version is set by scripts/build_cli.sh via -ldflags -X. Left at "dev" by a
// plain `go build`.
var Version = "dev"

// Commit is set by scripts/build_cli.sh on a tag build (-buildvcs=false there,
// so the bytes are identical wherever the tag is built). A dev build leaves it
// empty and Describe reads the VCS stamp instead.
var Commit = ""

// buildInfo is a seam so the tests can drive Describe through every shape a
// build can have (stamped, dirty, tarball) without cross-compiling anything.
var buildInfo = debug.ReadBuildInfo

// vcs is what runtime/debug stamped into the binary: the revision, whether
// the tree was dirty, and whether a stamp exists at all (a tarball build has
// none, and says so as "unknown" rather than inventing a sha).
func vcs() (revision string, modified bool, ok bool) {
	info, found := buildInfo()
	if !found {
		return "", false, false
	}
	for _, s := range info.Settings {
		switch s.Key {
		case "vcs.revision":
			revision = s.Value
		case "vcs.modified":
			modified = s.Value == "true"
		}
	}
	return revision, modified, revision != ""
}

func short(sha string) string {
	if len(sha) > 7 {
		return sha[:7]
	}
	return sha
}

// commit is the short sha the binary was built from, or "" when unknown.
func commit() (sha string, dirty bool) {
	if Commit != "" {
		return short(Commit), false
	}
	revision, modified, ok := vcs()
	if !ok {
		return "", false
	}
	return short(revision), modified
}

// Describe never returns a bare semver unless Version was set by ldflags:
// "0.3.0" | "dev+5a91734" | "dev+5a91734.dirty" | "dev+unknown".
func Describe() string {
	if Version != "dev" && Version != "" {
		return Version
	}
	sha, dirty := commit()
	if sha == "" {
		return "dev+unknown"
	}
	if dirty {
		return "dev+" + sha + ".dirty"
	}
	return "dev+" + sha
}

// Line is what `lazyaf --version` prints:
// "lazyaf 0.3.0 (commit 5a91734, go1.26.8, linux/amd64)". The second
// whitespace-separated token is the version, for scripts and the CI check
// (§4.3); nothing before it may contain a space.
func Line() string {
	sha, _ := commit()
	if sha == "" {
		sha = "unknown"
	}
	return fmt.Sprintf("lazyaf %s (commit %s, %s, %s/%s)",
		Describe(), sha, runtime.Version(), runtime.GOOS, runtime.GOARCH)
}

// Parse pulls the version token back out of a Line - the same rule the
// release check applies from outside, kept next to Line so the two cannot
// drift.
func Parse(line string) (string, bool) {
	fields := strings.Fields(line)
	if len(fields) < 2 || fields[0] != "lazyaf" {
		return "", false
	}
	return fields[1], true
}
