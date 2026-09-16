package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf debug list`. Port of cli.py debug_list (:1396-1407).
func newDebugListCmd() *cobra.Command {
	return &cobra.Command{
		Use:     "list",
		Short:   "List debug sessions that have not ended",
		Example: "  lazyaf debug list",
		Args:    exactArgs(),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := newClient()
			if err != nil {
				return err
			}
			var sessions []api.DebugSessionInfo
			if err := client.Get(cmd.Context(), "/api/debug", &sessions); err != nil {
				return err
			}
			if len(sessions) == 0 {
				ui.Out("No active debug sessions.")
				return nil
			}
			ui.Out("%d active debug session(s):", len(sessions))
			ui.Out("")
			for _, s := range sessions {
				where := ""
				if s.CurrentStep != nil {
					name := s.CurrentStep.Name
					if name == "" {
						name = s.CurrentStep.Key
					}
					where = " at " + name
				}
				ui.Out("  %s  %s%s", s.ID, s.Status, where)
			}
			return nil
		},
	}
}
