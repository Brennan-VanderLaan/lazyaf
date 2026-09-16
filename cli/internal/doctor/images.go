package doctor

import (
	"fmt"
	"strings"
	"time"
)

const (
	defaultImagePrefix = "ghcr.io/brennan-vanderlaan/lazyaf"
	defaultVersion     = "latest"

	// The GHCR path already ends in /lazyaf, so a step image published
	// there drops this prefix: lazyaf-base:dev is pulled as <prefix>/base
	// (preflight.py:52-55, .github/scripts/step_images.py).
	stepNamePrefix = "lazyaf-"
)

// serviceImages are the services the release workflow publishes, in the
// order they matter (preflight.py:38).
var serviceImages = []string{"backend", "frontend", "runner-agent"}

func imagePresentLocally(opts Options, ref string) bool {
	code, _ := opts.Exec.Run(defaultTimeout, "docker", "image", "inspect", ref)
	return code == 0
}

// remote is a tri-state answer from the registry.
type remote int

const (
	remoteUnknown remote = iota // could not tell: offline, private, old CLI
	remotePresent
	remoteAbsent
)

func imagePresentRemotely(opts Options, ref string) remote {
	code, out := opts.Exec.Run(45*time.Second, "docker", "manifest", "inspect", ref)
	if code == 0 {
		return remotePresent
	}
	lowered := strings.ToLower(out)
	if strings.Contains(lowered, "not found") || strings.Contains(lowered, "manifest unknown") || strings.Contains(lowered, "denied") {
		return remoteAbsent
	}
	return remoteUnknown
}

func imageCoordinates(values map[string]string) (prefix, version string) {
	prefix = strings.TrimRight(values["LAZYAF_IMAGE_PREFIX"], "/")
	if prefix == "" {
		prefix = defaultImagePrefix
	}
	version = values["LAZYAF_VERSION"]
	if version == "" {
		version = defaultVersion
	}
	return prefix, version
}

// checkServiceImages: the three service images the release compose pulls
// (preflight.py:542-602).
func checkServiceImages(r *reporter, values map[string]string, opts Options) {
	if opts.Dev {
		r.report(statusOK, "Service images: skipped (--dev builds them from source)",
			"docker compose build   then   docker compose up -d")
		return
	}
	prefix, version := imageCoordinates(values)
	var missing, unknown []string
	for _, service := range serviceImages {
		ref := fmt.Sprintf("%s/%s:%s", prefix, service, version)
		if imagePresentLocally(opts, ref) {
			r.report(statusOK, "Image present locally: "+ref)
			continue
		}
		if opts.Offline {
			unknown = append(unknown, ref)
			continue
		}
		switch imagePresentRemotely(opts, ref) {
		case remotePresent:
			r.report(statusOK, "Image available to pull: "+ref)
		case remoteAbsent:
			missing = append(missing, ref)
		default:
			unknown = append(unknown, ref)
		}
	}

	if len(missing) > 0 {
		details := indent(missing)
		details = append(details,
			"Check LAZYAF_VERSION in .env against the published tags:",
			"  https://github.com/Brennan-VanderLaan/lazyaf/pkgs/container/lazyaf%2Fbackend",
			"If no release has been published yet, build from source instead:",
			"  docker compose build && docker compose up -d",
		)
		r.report(statusFail, fmt.Sprintf("%d release image(s) do not exist at that name/tag", len(missing)), details...)
	}
	if len(unknown) > 0 && opts.Offline {
		details := indent(unknown)
		details = append(details, "Re-run without --offline, or just:", "  docker compose -f "+releaseCompose+" pull")
		r.report(statusWarn, fmt.Sprintf("%d image(s) not present locally, registry check skipped (--offline)", len(unknown)), details...)
	} else if len(unknown) > 0 {
		details := indent(unknown)
		details = append(details,
			"This is normal behind a proxy or on an older docker CLI.",
			"The pull itself is the real answer:",
			"  docker compose -f "+releaseCompose+" pull",
		)
		r.report(statusWarn, fmt.Sprintf("Could not verify %d image(s) from here", len(unknown)), details...)
	}
}

// checkStepImages: the lazyaf-*:dev images agent and control steps run in
// (preflight.py:605-651). The list is StepImages (step_images_gen.go),
// generated from scripts/build_images.py's IMAGES table - the script's own
// FALLBACK_STEP_IMAGES copy is gone (§8.3).
//
// These are referenced by the LOCAL tag from inside the backend
// (pipeline_executor maps claude-code -> lazyaf-claude:dev), so a pulled
// copy has to be retagged to that name. Nothing pulls them for you: a
// missing one fails a step loudly rather than pulling behind your back,
// and doctor pulls nothing either.
func checkStepImages(r *reporter, values map[string]string, opts Options) {
	var missing []string
	for _, ref := range StepImages {
		if !imagePresentLocally(opts, ref) {
			missing = append(missing, ref)
		}
	}
	if len(missing) == 0 {
		r.report(statusOK, fmt.Sprintf("All %d step images present as :dev", len(StepImages)))
		return
	}
	title := fmt.Sprintf("%d of %d step images missing", len(missing), len(StepImages))
	if opts.Dev {
		details := indent(missing)
		details = append(details, "Build them:", "  python scripts/build_images.py")
		r.report(statusWarn, title, details...)
		return
	}

	prefix, version := imageCoordinates(values)
	details := []string{
		"Pipeline steps and AI agent cards need these. The backend does NOT",
		"pull them for you - a missing one fails the step with a clear message.",
		"Pull and retag them to the local :dev name the backend looks for:",
	}
	for _, ref := range missing {
		name, _, _ := strings.Cut(ref, ":")
		// Building the remote ref from the LOCAL name yields
		// <prefix>/lazyaf-base, and every pull 404s; only the remote half
		// drops the prefix (preflight.py:633-641).
		remoteName := strings.TrimPrefix(name, stepNamePrefix)
		details = append(details,
			fmt.Sprintf("  docker pull %s/%s:%s", prefix, remoteName, version),
			fmt.Sprintf("  docker tag %s/%s:%s %s", prefix, remoteName, version, ref),
		)
	}
	r.report(statusWarn, title, details...)
	if opts.Offline {
		r.report(statusWarn, "(--offline: no registry lookup was attempted for the step images)")
	}
}

func indent(items []string) []string {
	out := make([]string, 0, len(items))
	for _, item := range items {
		out = append(out, "  "+item)
	}
	return out
}
