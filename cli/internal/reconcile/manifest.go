// Package reconcile is `lazyaf tests reconcile`: the declared test set, from
// a refs manifest or from a real `pytest --collect-only`.
//
// Port of cli/lazyaf/cli.py:860-1178. WHY THE REFUSALS (12.4 adversarial
// finding, tdd/unit/scripts/test_cli_tests_reconcile.py): reconcile ORPHANS
// every active TestRef absent from its input. The command used to default
// its input to a results manifest - the list of tests one tier's run
// happened to execute - and running it after a T1 lane silently orphaned
// every test T1 does not run. There is no safe default, so the command
// refuses ambiguity: no source, both sources, a results manifest without
// explicit opt-in, an empty declared set.
//
// The collector plugin (lazyaf_collect_plugin.py, embedded) is the one
// piece of Python the binary still carries (§1.3): it runs INSIDE the user's
// pytest, which is why it is Python and why this mode needs an interpreter.
package reconcile

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// Ref is one declared test: the wire shape of POST /api/test-refs/reconcile.
type Ref = api.ReconcileRefItem

// ResultsManifestHint is why a results manifest is refused (cli.py:854-858).
const ResultsManifestHint = "A results manifest lists only the tests that RAN in that invocation. " +
	"Reconciling from one ORPHANS every declared test the run did not touch " +
	"(a different tier, a -k filter, a failed collection)."

// Refusing prefixes every reconcile refusal, so `Refusing to reconcile` is
// greppable in a CI log exactly as it was in the Python CLI.
const Refusing = "Refusing to reconcile: "

// Options is the command's input after flag parsing.
type Options struct {
	RepoID string
	// Manifest is --refs/--manifest/-m.
	Manifest    string
	FromCollect bool
	// CollectPath is -C, the directory pytest collects in.
	CollectPath          string
	AllowResultsManifest bool
	// Python is --python; "" means resolve (ResolvePython).
	Python string
	// PytestArgs go to pytest verbatim after the collector flags.
	PytestArgs []string
}

// ValidateSource refuses the two ambiguous inputs before anything runs
// (cli.py:1153-1174). Deliberately blind to $LAZYAF_TEST_RESULTS_PATH and
// ./test_results.json: both were the old defaults, and honouring either
// reconciles one step's run against the whole repo.
func ValidateSource(o Options) error {
	if o.Manifest != "" && o.FromCollect {
		return ui.Fail(Refusing+"--refs and --from-collect are mutually exclusive - pick one source for the declared test set.",
			ui.WithRemedy(sourceRemedy))
	}
	if o.Manifest == "" && !o.FromCollect {
		return ui.Fail(Refusing+"no test-ref source given, and there is no safe default.",
			ui.WithDetail("Reconcile ORPHANS every active ref absent from its input, so "+
				"defaulting to a results manifest (./test_results.json, or "+
				"$LAZYAF_TEST_RESULTS_PATH from one tier's run) would silently "+
				"orphan every test that tier did not run."),
			ui.WithRemedy(sourceRemedy))
	}
	return nil
}

const sourceRemedy = "Pass exactly one of:\n" +
	"  --from-collect   run `pytest --collect-only` for the full declared set\n" +
	"  --refs <path>    an explicit refs manifest covering the whole suite"

// Classify says what shape a loaded manifest is: "results" is the pytest
// plugin's run output ({"version":1,"results":[...]}, pinned contract #1),
// "refs" a declared set ({"refs": [...]}), "list" a bare array of ref
// objects, "unknown" anything else (cli.py:860-878).
func Classify(data any) string {
	switch v := data.(type) {
	case map[string]any:
		if _, ok := v["results"]; ok {
			return "results"
		}
		if _, ok := v["refs"]; ok {
			return "refs"
		}
		return "unknown"
	case []any:
		return "list"
	default:
		return "unknown"
	}
}

// Normalize turns manifest entries into refs, dropping non-objects and
// blank ids and keeping the FIRST path seen for a duplicated id
// (cli.py:881-893).
func Normalize(entries []any) []Ref {
	refs := []Ref{}
	seen := map[string]bool{}
	for _, entry := range entries {
		obj, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		id, _ := obj["lazyaf_test_id"].(string)
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		ref := Ref{LazyafTestID: id}
		if p, ok := obj["file_path"].(string); ok {
			ref.FilePath = &p
		}
		refs = append(refs, ref)
	}
	return refs
}

// LoadManifest reads a refs manifest and normalises it (cli.py:896-953).
// Three shapes are accepted; the RESULTS shape only with allowResults, and
// then with a warning through warn, because a partial run silently orphans
// every test it did not execute.
func LoadManifest(path string, allowResults bool, warn func(summary string)) ([]Ref, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, ui.Fail(fmt.Sprintf("--refs manifest not found: %s", path),
			ui.WithDetail(err.Error()),
			ui.WithRemedy("Point --refs at a refs manifest, or build the declared set here and now:\n\n"+
				"    lazyaf tests reconcile <repo_id> --from-collect"))
	}
	var data any
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, ui.Fail(fmt.Sprintf("%s is not valid JSON", path), ui.WithDetail(err.Error()))
	}
	var entries []any
	switch Classify(data) {
	case "results":
		if !allowResults {
			return nil, ui.Fail(Refusing+fmt.Sprintf("%s is a test RESULTS manifest, not a refs manifest.", path),
				ui.WithDetail(ResultsManifestHint),
				ui.WithRemedy("Use one of:\n"+
					"  --from-collect            collect the full declared set with pytest\n"+
					"  --refs <path>             an explicit refs manifest ({'refs': [...]})\n"+
					"  --allow-results-manifest  only if it came from a FULL-suite run"))
		}
		if warn != nil {
			warn(fmt.Sprintf("reconciling from a results manifest (%s). %s", path, ResultsManifestHint))
		}
		entries, _ = data.(map[string]any)["results"].([]any)
	case "refs":
		entries, _ = data.(map[string]any)["refs"].([]any)
	case "list":
		entries = data.([]any)
	default:
		return nil, ui.Fail(fmt.Sprintf("%s has no 'refs' key and is not a list of ref objects", path),
			ui.WithRemedy("A refs manifest looks like:\n"+
				`    {"refs": [{"lazyaf_test_id": "us1.login", "file_path": "tests/test_login.py"}]}`))
	}
	return Normalize(entries), nil
}

// RequireNonEmpty refuses an empty declared set: sending it would orphan
// every active ref for the repo (cli.py:1197-1204).
func RequireNonEmpty(refs []Ref, source, repoID string) error {
	if len(refs) > 0 {
		return nil
	}
	return ui.Fail(Refusing+fmt.Sprintf("the declared set came back EMPTY (%s). Sending it would orphan every active ref for repo %s.", source, repoID),
		ui.WithRemedy("If that is genuinely intended, say so explicitly with a refs manifest containing an empty 'refs' list."))
}
