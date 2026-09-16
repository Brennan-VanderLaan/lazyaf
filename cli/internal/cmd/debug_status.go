package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
)

// `lazyaf debug status SESSION_ID`. Port of cli.py debug_status (:1442-1449).
func newDebugStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:               "status SESSION_ID",
		Short:             "Show one debug session",
		Example:           "  lazyaf debug status <session_id>",
		Args:              exactArgs("SESSION_ID"),
		ValidArgsFunction: completeSessionIDs,
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := newClient()
			if err != nil {
				return err
			}
			var s api.DebugSessionInfo
			if err := client.Get(cmd.Context(), "/api/debug/"+args[0], &s); err != nil {
				return err
			}
			printSession(s)
			return nil
		},
	}
}
