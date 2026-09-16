package version

import (
	"regexp"
	"runtime/debug"
	"strings"
	"testing"
)

// stamp swaps the ldflags values and the VCS stamp for one test and restores
// them, so the shapes a build can have (§4.2) are all reachable from one
// host binary.
func stamp(t *testing.T, version, commit, revision string, modified, found bool) {
	t.Helper()
	savedVersion, savedCommit, savedInfo := Version, Commit, buildInfo
	Version, Commit = version, commit
	buildInfo = func() (*debug.BuildInfo, bool) {
		if !found {
			return nil, false
		}
		info := &debug.BuildInfo{}
		if revision != "" {
			info.Settings = append(info.Settings, debug.BuildSetting{Key: "vcs.revision", Value: revision})
			mod := "false"
			if modified {
				mod = "true"
			}
			info.Settings = append(info.Settings, debug.BuildSetting{Key: "vcs.modified", Value: mod})
		}
		return info, true
	}
	t.Cleanup(func() { Version, Commit, buildInfo = savedVersion, savedCommit, savedInfo })
}

const fullSHA = "5a917340123456789abcdef0123456789abcdef0"

func TestDevBuildSaysDev(t *testing.T) {
	stamp(t, "dev", "", fullSHA, false, true)
	if got := Describe(); !strings.HasPrefix(got, "dev+") {
		t.Fatalf("a plain go build must describe itself as dev+..., got %q", got)
	}
	if got := Describe(); got != "dev+5a91734" {
		t.Fatalf("Describe() = %q, want dev+5a91734 (short sha from the VCS stamp)", got)
	}
}

func TestDirtyTreeIsSaidOutLoud(t *testing.T) {
	stamp(t, "dev", "", fullSHA, true, true)
	if got := Describe(); got != "dev+5a91734.dirty" {
		t.Fatalf("Describe() = %q, want dev+5a91734.dirty", got)
	}
}

func TestTarballBuildSaysUnknownRatherThanInventingASha(t *testing.T) {
	stamp(t, "dev", "", "", false, false)
	if got := Describe(); got != "dev+unknown" {
		t.Fatalf("Describe() = %q, want dev+unknown", got)
	}
	stamp(t, "dev", "", "", false, true) // a build info block with no vcs.* keys
	if got := Describe(); got != "dev+unknown" {
		t.Fatalf("Describe() with a stamp that has no revision = %q, want dev+unknown", got)
	}
}

var bareSemver = regexp.MustCompile(`^\d+\.\d+\.\d+$`)

func TestDescribeNeverBareSemverWhenDev(t *testing.T) {
	for _, tc := range []struct {
		name     string
		revision string
		found    bool
	}{
		{"stamped", fullSHA, true},
		{"no stamp", "", false},
		{"empty stamp", "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stamp(t, "dev", "", tc.revision, false, tc.found)
			if got := Describe(); bareSemver.MatchString(got) {
				t.Fatalf("Describe() = %q: a dev build claimed a release version", got)
			}
		})
	}
	// An empty Version (a broken -X) is treated as dev, not as "".
	stamp(t, "", "", fullSHA, false, true)
	if got := Describe(); !strings.HasPrefix(got, "dev+") {
		t.Fatalf("Describe() with an empty Version = %q, want dev+...", got)
	}
}

func TestTagBuildIsTheTag(t *testing.T) {
	stamp(t, "0.3.0", fullSHA, "deadbeefdeadbeefdeadbeef", true, true)
	if got := Describe(); got != "0.3.0" {
		t.Fatalf("Describe() = %q, want the ldflags version verbatim", got)
	}
	// The explicit Commit wins over the VCS stamp, and the dirty bit of the
	// build host does not leak into a release line (§4.2: identical bytes
	// wherever the tag is built).
	if line := Line(); !strings.Contains(line, "(commit 5a91734, ") {
		t.Fatalf("Line() = %q, want the ldflags commit", line)
	}
}

func TestLineIsParseable(t *testing.T) {
	for _, tc := range []struct {
		name, version, commit, revision string
		want                            string
	}{
		{"tag", "0.3.0", fullSHA, "", "0.3.0"},
		{"dev", "dev", "", fullSHA, "dev+5a91734"},
		{"tarball", "dev", "", "", "dev+unknown"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stamp(t, tc.version, tc.commit, tc.revision, false, tc.revision != "")
			line := Line()
			fields := strings.Fields(line)
			if len(fields) < 2 || fields[0] != "lazyaf" || fields[1] != tc.want {
				t.Fatalf("Line() = %q: the second token must be the version %q", line, tc.want)
			}
			if fields[1] != Describe() {
				t.Fatalf("Line() token %q != Describe() %q", fields[1], Describe())
			}
			got, ok := Parse(line)
			if !ok || got != tc.want {
				t.Fatalf("Parse(%q) = %q, %v; want %q", line, got, ok, tc.want)
			}
			if !strings.Contains(line, "(commit ") || !strings.HasSuffix(line, ")") {
				t.Fatalf("Line() = %q, want the (commit X, goN, os/arch) tail", line)
			}
		})
	}
	if _, ok := Parse("something else"); ok {
		t.Fatal("Parse accepted a line that is not a lazyaf version line")
	}
}
