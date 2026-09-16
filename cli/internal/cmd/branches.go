package cmd

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf branches REPO_ID`. Port of cli.py branches (:845-880).
func newBranchesCmd() *cobra.Command {
	return &cobra.Command{
		Use:               "branches REPO_ID",
		Short:             "List branches in a LazyAF repo",
		Example:           "  lazyaf branches abc123",
		Args:              exactArgs("REPO_ID"),
		ValidArgsFunction: completeRepoIDs,
		RunE: func(cmd *cobra.Command, args []string) error {
			repoID := args[0]
			client, err := newClient()
			if err != nil {
				return err
			}
			var data api.Branches
			if err := client.Get(cmd.Context(), "/api/repos/"+repoID+"/branches", &data,
				api.NotFound(fmt.Sprintf("repo %s does not exist on the LazyAF backend. `lazyaf list` shows the ids that do.", repoID))); err != nil {
				return err
			}
			if len(data.Branches) == 0 {
				// Not an error - a registered repo legitimately has no refs
				// until something is pushed - but it IS the state that makes
				// agents fail later, so it names the fix rather than just
				// the fact.
				ui.Out("Repo %s has no branches yet: nothing has been pushed to it, so an agent would have nothing to check out.", repoID)
				ui.Out("Push your code with:  lazyaf ingest <path> --name <name>")
				return nil
			}
			ui.Out("Branches in repo (%d):", data.Total)
			ui.Out("")
			for _, b := range data.Branches {
				var markers []string
				if b.IsDefault {
					markers = append(markers, "default")
				}
				if b.IsLazyaf {
					markers = append(markers, "lazyaf")
				}
				commit := b.Commit
				if len(commit) > 8 {
					commit = commit[:8]
				}
				ui.Out("  %s  %s  %s", b.Name, commit, strings.Join(markers, " "))
			}
			return nil
		},
	}
}
