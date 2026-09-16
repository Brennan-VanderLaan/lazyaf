package cmd

import (
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/doctor"
)

// `lazyaf doctor`. The command that replaces scripts/preflight.py
// (upcoming/go-cli.md §8.2). The checks live in internal/doctor (L4); this
// file is the cobra surface only. §5's "new --server URL for the backend
// check" IS the root's persistent --server/-s: doctor.Options.Server takes
// ServerFlag and api.New resolves flag > $LAZYAF_SERVER > default with the
// same provenance line every other command prints, so there is one
// resolution rule, not two.
func newDoctorCmd() *cobra.Command {
	var opts doctor.Options
	c := &cobra.Command{
		Use:   "doctor",
		Short: "Check this machine is ready to run LazyAF, before you start it",
		Long: "Every check prints one line and, when something is wrong, the exact command\n" +
			"that fixes it. Nothing is modified: no pulls, no writes, no containers. .env\n" +
			"is read for whether a key is set and its shape; no value is ever printed.\n" +
			"Exit 0 = good to go, 1 = fix something first; warnings do not fail the run.",
		Example: "  lazyaf doctor\n" +
			"  lazyaf doctor --dev\n" +
			"  lazyaf doctor --offline --dir ~/lazyaf-release\n" +
			"  lazyaf doctor --server http://localhost:8790",
		Args: exactArgs(),
		RunE: func(cmd *cobra.Command, args []string) error {
			// doctor resolves the URL itself for its provenance line, but an
			// EXPLICIT empty --server must refuse here exactly as every other
			// command does (V4-2): passing "" through would make doctor fall
			// back to $LAZYAF_SERVER and report a server nobody asked about.
			if _, err := api.ResolveServer(serverSetting()); err != nil {
				return err
			}
			opts.Server = ServerFlag
			return doctor.Run(opts, cmd.OutOrStdout())
		},
	}
	f := c.Flags()
	f.BoolVar(&opts.Dev, "dev", false,
		"check the build-from-source stack (docker-compose.yml) instead")
	f.BoolVar(&opts.Offline, "offline", false,
		"skip every registry lookup and the backend probe (no network calls)")
	f.StringVar(&opts.Dir, "dir", "",
		"LazyAF directory to check (default: the current directory)")
	// --debug-trace is for a bug report against doctor itself (§8.2 "never
	// a traceback"): hidden so --help stays about the operator's setup.
	f.BoolVar(&opts.DebugTrace, "debug-trace", false,
		"include the stack trace if doctor itself hits a bug")
	_ = f.MarkHidden("debug-trace")
	_ = c.MarkFlagDirname("dir")
	return c
}
