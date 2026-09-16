package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf debug abort SESSION_ID`. Port of cli.py debug_abort (:1544-1556).
func newDebugAbortCmd() *cobra.Command {
	return &cobra.Command{
		Use:               "abort SESSION_ID",
		Short:             "End the session AND cancel its pipeline run",
		Example:           "  lazyaf debug abort <session_id>",
		Args:              exactArgs("SESSION_ID"),
		ValidArgsFunction: completeSessionIDs,
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := newClient()
			if err != nil {
				return err
			}
			var data api.DebugAbortResponse
			if err := client.Post(cmd.Context(), "/api/debug/"+args[0]+"/abort", nil, &data); err != nil {
				return err
			}
			ui.Out("Aborted. Session is %s (%s); the run was cancelled.", data.Status, data.EndReason)
			return nil
		},
	}
}
