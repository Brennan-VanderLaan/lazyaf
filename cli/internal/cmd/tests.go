package cmd

import "github.com/spf13/cobra"

// `lazyaf tests` - test tie-back commands (Phase 12.2.6). Port of the click
// group at cli.py:849-853.
func newTestsCmd() *cobra.Command {
	g := newGroup("tests", "Test tie-back commands (Phase 12.2.6)",
		"Test tie-back commands (Phase 12.2.6).",
		"  lazyaf tests reconcile <repo_id> --from-collect")
	g.AddCommand(newTestsReconcileCmd())
	return g
}
