package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf debug resume SESSION_ID [--all]`. Port of cli.py debug_resume
// (:1515-1541).
func newDebugResumeCmd() *cobra.Command {
	var clearRemaining bool
	cmd := &cobra.Command{
		Use:               "resume SESSION_ID",
		Short:             "Release a paused step and continue to the next breakpoint",
		Example:           "  lazyaf debug resume <session_id>\n  lazyaf debug resume <session_id> --all",
		Args:              exactArgs("SESSION_ID"),
		ValidArgsFunction: completeSessionIDs,
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := newClient()
			if err != nil {
				return err
			}
			var data api.DebugResumeResponse
			if err := client.Post(cmd.Context(), "/api/debug/"+args[0]+"/resume",
				api.DebugResumeRequest{ClearRemaining: clearRemaining}, &data); err != nil {
				return err
			}
			if next := deref(data.NextBreakpoint); next != "" {
				ui.Out("Resumed. Session is %s; next breakpoint: %s", data.Status, next)
			} else {
				ui.Out("Resumed. Session is %s; no breakpoints left.", data.Status)
			}
			return nil
		},
	}
	cmd.Flags().BoolVar(&clearRemaining, "all", false, "Drop the remaining breakpoints and run to completion")
	return cmd
}
