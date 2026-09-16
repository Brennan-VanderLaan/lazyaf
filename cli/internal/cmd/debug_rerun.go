package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf debug rerun RUN_ID [--break KEY]... [--commit C] [--branch B]
// [--timeout S]`. Port of cli.py debug_rerun (:1335-1390). Not completable:
// there is no run-listing endpoint (§7, stated).
func newDebugRerunCmd() *cobra.Command {
	var (
		breakpoints []string
		commit      string
		branch      string
		timeout     int
	)
	cmd := &cobra.Command{
		Use:   "rerun RUN_ID",
		Short: "Re-run a pipeline run with breakpoints",
		Long: "Re-run a pipeline run with breakpoints.\n\n" +
			"The re-run carries ONLY the original run's branch and commit. on_pass /\n" +
			"on_fail actions and card routing are deliberately dropped, so a debug\n" +
			"re-run can never merge a branch and never moves a card.",
		Example: "  lazyaf debug rerun <run_id> --break build\n" +
			"  lazyaf debug rerun <run_id> --break build --break test --timeout 900",
		Args:              exactArgs("RUN_ID"),
		ValidArgsFunction: cobra.NoFileCompletions,
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := newClient()
			if err != nil {
				return err
			}
			body := api.DebugRerunRequest{
				Breakpoints:       append([]string{}, breakpoints...),
				UseOriginalCommit: commit == "" && branch == "",
			}
			if commit != "" {
				body.CommitSHA = &commit
			}
			if branch != "" {
				body.Branch = &branch
			}
			if timeout != 0 {
				body.TimeoutSeconds = &timeout
			}
			var data api.DebugRerunResponse
			if err := client.Post(cmd.Context(), "/api/pipeline-runs/"+args[0]+"/debug-rerun", body, &data); err != nil {
				return err
			}
			ui.Out("")
			ui.Out("Debug re-run started")
			ui.Out("")
			ui.Out("run:     %s", data.RunID)
			ui.Out("session: %s", data.DebugSessionID)
			ui.Out("")
			ui.Out("%s", data.JoinCommand)
			return nil
		},
	}
	cmd.Flags().StringArrayVar(&breakpoints, "break", nil,
		"Pause BEFORE this step, named by its step id in the pipeline graph (NOT its position - index keys were the v1 array's address and are retired). Repeatable. An unknown key is refused, so a breakpoint either fires or says why it cannot.")
	cmd.Flags().StringVar(&commit, "commit", "", "Re-run at this commit instead of the original")
	cmd.Flags().StringVar(&branch, "branch", "", "Re-run on this branch instead of the original")
	cmd.Flags().IntVar(&timeout, "timeout", 0, "How long a breakpoint pause may wait (seconds, clamped to 4h)")
	_ = cmd.RegisterFlagCompletionFunc("break", cobra.NoFileCompletions) // §7: step keys are not completed, stated
	_ = cmd.RegisterFlagCompletionFunc("commit", cobra.NoFileCompletions)
	_ = cmd.RegisterFlagCompletionFunc("branch", cobra.NoFileCompletions)
	return cmd
}
