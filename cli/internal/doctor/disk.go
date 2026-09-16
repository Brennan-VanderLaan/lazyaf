package doctor

import "fmt"

// Free space we want available to docker: images plus a couple of
// workspaces (preflight.py:57-59).
const (
	diskWarnGB = 15
	diskFailGB = 5
)

const diskNote = "On Windows and macOS, Docker Desktop stores images in its own VM disk, " +
	"which may live elsewhere - check Docker Desktop > Settings > Resources " +
	"if this passes but pulls still fail with 'no space left on device'."

// diskVerdict is the threshold rule alone, so TestDiskThresholds can pin
// it without a filesystem (preflight.py:146-168).
func diskVerdict(freeGB float64) (status, string, []string) {
	where := "on the drive holding this directory"
	switch {
	case freeGB < diskFailGB:
		return statusFail, fmt.Sprintf("Only %.1f GB free %s", freeGB, where), []string{
			fmt.Sprintf("The LazyAF images need roughly %d GB. Free some space first.", diskWarnGB),
			diskNote,
		}
	case freeGB < diskWarnGB:
		return statusWarn, fmt.Sprintf("%.1f GB free %s", freeGB, where), []string{
			fmt.Sprintf("That is enough to start, but %d GB is more comfortable once step", diskWarnGB),
			"images and workspaces accumulate.",
			diskNote,
		}
	default:
		return statusOK, fmt.Sprintf("%.1f GB free %s", freeGB, where), nil
	}
}

// checkDisk (preflight.py:138-168). An unreadable figure is a WARN, never
// a stop: it is advice about headroom, not a requirement.
func checkDisk(r *reporter, dir string, free func(string) (uint64, error)) {
	bytes, err := free(dir)
	if err != nil {
		r.report(statusWarn, "Could not read free disk space", err.Error())
		return
	}
	s, title, details := diskVerdict(float64(bytes) / (1 << 30))
	r.report(s, title, details...)
}
