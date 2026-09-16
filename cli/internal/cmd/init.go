package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/initcmd"
)

// `lazyaf init`. The command that replaces scripts/bootstrap_secrets.py
// (upcoming/go-cli.md §8.1). The work lives in internal/initcmd (L4); this
// file is the cobra surface only: flags, Example, and the RunE that hands
// initcmd.Run the options. initcmd.Run returns a *ui.Failure for every
// refusal (a missing secret under --check is exit 1), which Run reports
// like every other command's.
func newInitCmd() *cobra.Command {
	var opts initcmd.Options
	c := &cobra.Command{
		Use:   "init",
		Short: "Put real, random shared secrets in your .env - once, and then never again",
		Long: "Writes strong random values for exactly the keys that are missing\n" +
			"(LAZYAF_STEP_AUTH_SECRET, LAZYAF_RUNNER_AUTH_SECRET) and leaves everything\n" +
			"else alone. Never overwrites a value you set, never prints a secret, never\n" +
			"generates for a key you pointed at a file with <NAME>_FILE. Safe to re-run\n" +
			"and to run concurrently.",
		Example: "  lazyaf init\n  lazyaf init --check\n  lazyaf init --env-file /srv/lazyaf/.env",
		Args:    exactArgs(),
		RunE: func(cmd *cobra.Command, args []string) error {
			return initcmd.Run(opts, cmd.OutOrStdout())
		},
	}
	f := c.Flags()
	f.StringVar(&opts.Dir, "dir", "",
		"LazyAF directory holding .env (default: the current directory; must contain .env.example or docker-compose*.yml)")
	f.StringVar(&opts.EnvFile, "env-file", "",
		"path to the .env to update (default: <dir>/.env; no marker needed when given)")
	f.StringVar(&opts.Template, "template", "",
		"template to seed a missing .env from (default: <dir>/.env.example)")
	f.BoolVar(&opts.Check, "check", false,
		"report which secrets are missing and change nothing (exit 1 if any)")
	_ = c.MarkFlagDirname("dir")
	_ = c.MarkFlagFilename("env-file")
	_ = c.MarkFlagFilename("template")
	return c
}
