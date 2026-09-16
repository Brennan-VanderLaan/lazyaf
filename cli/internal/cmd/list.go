package cmd

import (
	"strings"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf list`. Port of cli.py list_repos (:816-841). Also the quickest way
// to see WHICH backend this shell is pointed at: the URL and where it came
// from are printed above the results.
func newListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List all repos in LazyAF",
		Long: "List all repos in LazyAF.\n\n" +
			"Also the quickest way to see WHICH backend this shell is pointed at -\n" +
			"the URL and where it came from are printed above the results.",
		Example: "  lazyaf list\n  lazyaf list --server http://localhost:8000",
		Args:    exactArgs(),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := newClient()
			if err != nil {
				return err
			}
			var repos []api.Repo
			if err := client.Get(cmd.Context(), "/api/repos", &repos); err != nil {
				return err
			}
			// The first line of Describe: "LazyAF backend: <url> (from <source>)".
			ui.Out("%s", strings.SplitN(client.Describe(), "\n", 2)[0])
			if len(repos) == 0 {
				ui.Out("No repos found. Use lazyaf ingest to add one.")
				return nil
			}
			ui.Out("Found %d repo(s):", len(repos))
			ui.Out("")
			for _, r := range repos {
				status := "ingested"
				if !r.IsIngested {
					status = "not ingested"
				}
				ui.Out("  %s  %s  %s", r.ID, r.Name, status)
				if u := deref(r.RemoteURL); u != "" {
					ui.Out("    Remote: %s", u)
				}
			}
			return nil
		},
	}
}
