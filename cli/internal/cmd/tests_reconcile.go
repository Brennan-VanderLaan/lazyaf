package cmd

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/reconcile"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf tests reconcile REPO_ID [--refs P | --from-collect [-C DIR]
// [--python P]] [-- PYTEST_ARGS...]`. Port of cli.py reconcile (:1139-1247).
//
// Stated deviation from the click twin: click's `ignore_unknown_options`
// let dash-prefixed pytest options ride after the flags; pflag drops unknown
// flags rather than passing them through, so pytest OPTIONS go after `--`
// (positional pytest paths work in the same place they always did).
func newTestsReconcileCmd() *cobra.Command {
	var o reconcile.Options
	cmd := &cobra.Command{
		Use:   "reconcile REPO_ID [-- PYTEST_ARGS...]",
		Short: "Reconcile a repo's TestRefs against its FULL declared test set",
		Long: "Reconcile a repo's TestRefs against its FULL declared test set.\n\n" +
			"Listed refs are upserted to active (with file_path); previously-active\n" +
			"refs for the repo that are ABSENT from the input flip to ORPHAN. That\n" +
			"orphaning is why the input must be the whole declared set - so there is\n" +
			"no default source, and exactly one of --refs / --from-collect is\n" +
			"required.\n\n" +
			"--from-collect runs a collector INSIDE your pytest, so it is the one\n" +
			"lazyaf command that needs a Python interpreter: --python, then\n" +
			"$" + reconcile.PythonEnvVar + ", then $VIRTUAL_ENV, then python3 / python (/ py -3 on\n" +
			"Windows). The \"Collecting:\" line names the one it chose. pytest options\n" +
			"go after `--`; pytest paths may follow the flags directly.",
		Example: "  lazyaf tests reconcile abc123 --from-collect\n" +
			"  lazyaf tests reconcile abc123 --from-collect -C backend ../tdd\n" +
			"  lazyaf tests reconcile abc123 --from-collect -C backend -- ../tdd -k login\n" +
			"  lazyaf tests reconcile abc123 --refs refs.json",
		Args: cobra.ArbitraryArgs, // REPO_ID then pytest paths; checked in RunE
		ValidArgsFunction: func(cmd *cobra.Command, args []string, toComplete string) ([]cobra.Completion, cobra.ShellCompDirective) {
			if len(args) == 0 {
				return completeRepoIDs(cmd, args, toComplete)
			}
			return nil, cobra.ShellCompDirectiveDefault // pytest paths
		},
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 {
				return ui.Usage("Missing argument 'REPO_ID'.", ui.WithRemedy(usageRemedy(cmd)))
			}
			o.RepoID = args[0]
			o.PytestArgs = args[1:]
			return runReconcile(cmd, o)
		},
	}
	f := cmd.Flags()
	f.StringVarP(&o.Manifest, "refs", "m", "",
		"Path to a REFS manifest: {'refs': [{lazyaf_test_id, file_path}]} or a bare JSON list. Must describe the repo's FULL declared test set.")
	// --manifest is kept as an alias of --refs (cli.py:1142); it must not be
	// a way back into the old behaviour, so it is the same variable.
	var manifestAlias string
	f.StringVar(&manifestAlias, "manifest", "", "Alias of --refs")
	_ = f.MarkHidden("manifest")
	// No backticks in this usage string: pflag's UnquoteUsage reads a
	// backtick-quoted word as the flag's VALUE NAME, so `pytest
	// --collect-only` rendered a bool flag as if it took that argument
	// (verifier finding V4-3).
	f.BoolVar(&o.FromCollect, "from-collect", false,
		"Build the full declared set by running pytest --collect-only over the suite (see --collect-path; pytest args after -- are passed through).")
	f.StringVarP(&o.CollectPath, "collect-path", "C", ".", "Directory to run collection in with --from-collect")
	f.BoolVar(&o.AllowResultsManifest, "allow-results-manifest", false,
		"Permit a pytest RESULTS manifest as --refs input. Only correct when it came from a FULL-suite run: a partial run orphans everything it did not execute.")
	f.StringVar(&o.Python, "python", "",
		"Python interpreter for --from-collect (default: $"+reconcile.PythonEnvVar+", $VIRTUAL_ENV, python3, python, py -3)")
	_ = cmd.MarkFlagFilename("refs", "json")
	_ = cmd.MarkFlagFilename("manifest", "json")
	_ = cmd.MarkFlagDirname("collect-path")
	_ = cmd.MarkFlagFilename("python")
	cmd.PreRunE = func(cmd *cobra.Command, args []string) error {
		if manifestAlias != "" {
			if o.Manifest != "" && o.Manifest != manifestAlias {
				return ui.Usage("--refs and --manifest are the same option; give it once", ui.WithRemedy(usageRemedy(cmd)))
			}
			o.Manifest = manifestAlias
		}
		return nil
	}
	return cmd
}

func runReconcile(cmd *cobra.Command, o reconcile.Options) error {
	ctx := cmd.Context()
	// Validate the backend URL before collecting: `--from-collect` runs the
	// whole suite's collection, and finding out afterwards that
	// $LAZYAF_SERVER was mistyped wastes all of it.
	client, err := newClient()
	if err != nil {
		return err
	}
	if err := reconcile.ValidateSource(o); err != nil {
		return err
	}

	var (
		refs   []reconcile.Ref
		source string
	)
	if o.FromCollect {
		// click.Path(exists=True, file_okay=False, resolve_path=True) on -C.
		abs, err := filepath.Abs(o.CollectPath)
		if err == nil {
			o.CollectPath = abs
		}
		if st, err := os.Stat(o.CollectPath); err != nil || !st.IsDir() {
			return ui.Usage(fmt.Sprintf("Invalid value for '--collect-path' / '-C': directory '%s' does not exist.", o.CollectPath),
				ui.WithRemedy(usageRemedy(cmd)))
		}
		refs, err = reconcile.Collect(ctx, o, func(line string) { ui.Out("%s", line) })
		if err != nil {
			return err
		}
		source = fmt.Sprintf("pytest --collect-only in %s", o.CollectPath)
	} else {
		refs, err = reconcile.LoadManifest(o.Manifest, o.AllowResultsManifest, func(s string) { ui.Warn(s, "") })
		if err != nil {
			return err
		}
		source = o.Manifest
	}
	if err := reconcile.RequireNonEmpty(refs, source, o.RepoID); err != nil {
		return err
	}

	ui.Out("Reconciling %d test ref(s) for repo %s from %s", len(refs), o.RepoID, source)
	var result api.ReconcileResponse
	if err := client.Post(ctx, "/api/test-refs/reconcile",
		api.ReconcileRequest{RepoID: o.RepoID, Refs: refs}, &result,
		api.NotFound(fmt.Sprintf("repo %s does not exist on the LazyAF backend, so there are no test refs to reconcile. `lazyaf list` shows the ids that do.", o.RepoID))); err != nil {
		return err
	}
	ui.Out("")
	ui.Out("Reconciled!")
	ui.Out("")
	ui.Out("created: %d", result.Created)
	ui.Out("updated: %d", result.Updated)
	ui.Out("orphaned: %d", result.Orphaned)
	return nil
}
