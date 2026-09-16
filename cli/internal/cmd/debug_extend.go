package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf debug extend SESSION_ID [--minutes N]`. Port of cli.py
// debug_extend (:1560-1580).
func newDebugExtendCmd() *cobra.Command {
	var minutes int
	cmd := &cobra.Command{
		Use:               "extend SESSION_ID",
		Short:             "Push out a paused session's deadline",
		Example:           "  lazyaf debug extend <session_id> --minutes 60",
		Args:              exactArgs("SESSION_ID"),
		ValidArgsFunction: completeSessionIDs,
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := newClient()
			if err != nil {
				return err
			}
			var data api.DebugExtendResponse
			if err := client.Post(cmd.Context(), "/api/debug/"+args[0]+"/extend",
				api.DebugExtendRequest{AdditionalMinutes: minutes}, &data); err != nil {
				return err
			}
			ui.Out("Extended. Expires at %s", data.ExpiresAt)
			if data.Clamped {
				ui.Out("Clamped to the session's maximum lifetime (max_timeout_seconds).")
			}
			return nil
		},
	}
	cmd.Flags().IntVar(&minutes, "minutes", 30, "Minutes to add (1-180)")
	return cmd
}
