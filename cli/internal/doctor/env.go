package doctor

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/envfile"
)

// The two secrets the backend REFUSES to start without (preflight.py:
// 304-309). The purposes are the script's own words; the keys are
// envfile.ManagedSecrets so init and doctor cannot disagree about which
// keys exist.
var sharedSecretPurpose = map[string]string{
	"LAZYAF_STEP_AUTH_SECRET":   "signs the JWTs step containers use for /api/steps/*",
	"LAZYAF_RUNNER_AUTH_SECRET": "enrols runner agents at /ws/runner",
}

// minSecretLength: shorter than this is set but guessable (preflight.py:309).
const minSecretLength = 32

// checkEnv: .env exists, is ignored by git, and holds usable keys
// (preflight.py:229-282). Returns the parsed values for the checks that
// follow, or nil when there is no file.
func checkEnv(r *reporter, dir string, opts Options) map[string]string {
	envPath := filepath.Join(dir, ".env")
	_, exampleErr := os.Stat(filepath.Join(dir, ".env.example"))

	values, err := envfile.ReadFileValues(envPath)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			r.report(statusFail, "Could not read .env", err.Error())
			return nil
		}
		details := []string{
			"One command creates it AND generates the shared auth secrets the",
			"backend refuses to start without:",
			"  lazyaf init",
			"Then open .env and paste in your API keys.",
		}
		if exampleErr == nil {
			details = append(details, "(The stack starts without API keys, but no AI agent will run.)")
		}
		r.report(statusFail, ".env not found", details...)
		return nil
	}
	r.report(statusOK, fmt.Sprintf(".env found (%d variables set)", len(values)))

	// A committed .env is the one mistake that cannot be undone quietly.
	switch code, _ := opts.Exec.Run(10*time.Second, "git", "-C", dir, "check-ignore", "-q", ".env"); code {
	case 0:
		r.report(statusOK, ".env is gitignored - it will not be committed")
	case 1:
		r.report(statusFail,
			".env is NOT ignored by git",
			"Committing it would publish your API keys. Add '.env' to .gitignore",
			"before you run any git command in this repo.",
		)
	default:
		// 127/128: not a git checkout, or no git. Not worth a line either way.
	}

	anthStatus, anthMsg := describeKey("ANTHROPIC_API_KEY", values["ANTHROPIC_API_KEY"], "sk-ant-", "Claude")
	gemStatus, gemMsg := describeKey("GEMINI_API_KEY", values["GEMINI_API_KEY"], "", "Gemini")
	r.report(anthStatus, anthMsg)
	r.report(gemStatus, gemMsg)
	if anthStatus != statusOK && gemStatus != statusOK {
		r.report(statusWarn,
			"No usable AI provider key",
			"Repos, cards, pipelines, shell/docker steps and the git server all",
			"work without one. Agent steps and the playground will not.",
			"  ANTHROPIC_API_KEY -> https://console.anthropic.com/",
			"  GEMINI_API_KEY    -> https://aistudio.google.com/apikey",
		)
	}

	checkSharedSecrets(r, values, opts.Getenv)
	return values
}

var xRun = regexp.MustCompile(`x{4,}`)

// looksLikePlaceholder is the obvious "I did not fill this in" shapes of an
// API key (preflight.py:196-210). A different, smaller table than the
// shared-secret one: an API key is pasted, not generated.
func looksLikePlaceholder(value string) bool {
	if value == "" {
		return true
	}
	lowered := strings.ToLower(value)
	if xRun.MatchString(lowered) {
		return true
	}
	switch lowered {
	case "changeme", "your-key-here", "your_key_here", "todo", "none", "<your-key>":
		return true
	}
	return false
}

// describeKey is the verdict for one API key. SHAPE ONLY - never the value
// (preflight.py:213-226).
func describeKey(name, value, expectedPrefix, provider string) (status, string) {
	if value == "" {
		return statusWarn, fmt.Sprintf("%s is not set - %s agents will not run", name, provider)
	}
	if looksLikePlaceholder(value) {
		return statusFail, fmt.Sprintf("%s still holds a placeholder, not a real key", name)
	}
	if expectedPrefix != "" && !strings.HasPrefix(value, expectedPrefix) {
		return statusWarn, fmt.Sprintf("%s is set but does not start with '%s' - double-check you pasted the right value", name, expectedPrefix)
	}
	if len(value) < 20 {
		return statusWarn, fmt.Sprintf("%s is set but looks too short to be a real key", name)
	}
	return statusOK, fmt.Sprintf("%s is set and has the expected shape", name)
}

// checkSharedSecrets (preflight.py:327-389). A FAIL, not a warning: since
// 12.7 the stack does not come up without them. The _FILE form wins at
// resolution time, so it satisfies the check; whether the path resolves
// inside the container is not knowable from here.
func checkSharedSecrets(r *reporter, values map[string]string, getenv func(string) string) {
	var missing, retired, short []string
	for _, m := range envfile.ManagedSecrets {
		fileVar := m.Key + "_FILE"
		pointer := strings.TrimSpace(values[fileVar])
		if pointer == "" {
			pointer = strings.TrimSpace(getenv(fileVar))
		}
		if pointer != "" {
			r.report(statusOK, fmt.Sprintf("%s supplied by %s (%s)", m.Key, fileVar, pointer))
			continue
		}
		value := strings.TrimSpace(values[m.Key])
		if value == "" {
			value = strings.TrimSpace(getenv(m.Key))
		}
		switch {
		case envfile.IsRetiredPublicSecret(value):
			retired = append(retired, m.Key)
		case envfile.IsPlaceholder(value):
			missing = append(missing, m.Key)
		case len(value) < minSecretLength:
			short = append(short, m.Key)
		default:
			r.report(statusOK, fmt.Sprintf("%s is set (%s)", m.Key, sharedSecretPurpose[m.Key]))
		}
	}

	if len(retired) > 0 {
		r.report(statusFail,
			"Retired PUBLIC default still in .env for: "+strings.Join(retired, ", "),
			"That value shipped in LazyAF's source, so it is published - anyone",
			"can use it to mint credentials this backend would trust. The backend",
			"treats it as unset and will not start.",
			"Replace it with a generated value:",
			"  lazyaf init",
			"(it replaces the retired default and leaves everything else alone)",
		)
	}
	if len(missing) > 0 {
		r.report(statusFail,
			"Not set: "+strings.Join(missing, ", "),
			"The backend REFUSES TO START without these. There is deliberately no",
			"default: a default compiled into the source is a published secret.",
			"Generate them - it is one command, it never overwrites anything you",
			"already set, and it prints no values:",
			"  lazyaf init",
			"Delivering secrets by mounted file instead? Set the _FILE form",
			"(LAZYAF_STEP_AUTH_SECRET_FILE=...) - it takes precedence.",
		)
	}
	if len(short) > 0 {
		r.report(statusWarn,
			fmt.Sprintf("Under %d characters: %s", minSecretLength, strings.Join(short, ", ")),
			"Set, so the stack will start - but these mint credentials the",
			"backend trusts, and a short one is guessable. Prefer a generated",
			"value:  lazyaf init",
			"(it will NOT replace what you have; clear the line first if you",
			"want it regenerated)",
		)
	}
}
