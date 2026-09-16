package cmd

import (
	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf debug` - debug re-run commands (Phase 12.7). Port of the click
// group at cli.py:1284-1290.
//
// A GROUP, not `lazyaf debug <id> --resume`: a flag that changes the verb is
// not a flag, a group gives real per-verb --help, and `lazyaf tests
// reconcile` is the precedent. Every verb but attach is plain HTTP:
// controlling a session - resuming it, aborting it, extending its deadline -
// never depends on having a TTY, which is also why the terminal's
// `@`-commands are a convenience rather than the only way in.
func newDebugCmd(d *Deps) *cobra.Command {
	g := newGroup("debug", "Debug re-run commands (Phase 12.7)",
		"Debug re-run commands (Phase 12.7).",
		"  lazyaf debug rerun <run_id> --break build\n  lazyaf debug attach <session_id>")
	g.AddCommand(
		newDebugRerunCmd(),
		newDebugListCmd(),
		newDebugStatusCmd(),
		newDebugAttachCmd(d),
		newDebugResumeCmd(),
		newDebugAbortCmd(),
		newDebugExtendCmd(),
	)
	return g
}

// printSession is `lazyaf debug status`'s block (cli.py:1410-1439).
func printSession(s api.DebugSessionInfo) {
	ui.Out("status:      %s", s.Status)
	ui.Out("run:         %s", s.PipelineRunID)
	ui.Out("breakpoints: %s", joinOrNone(s.Breakpoints))
	ui.Out("  hit:       %s", joinOrNone(s.BreakpointsHit))
	ui.Out("  pending:   %s", joinOrNone(s.BreakpointsPending))
	if s.CurrentStep != nil {
		ui.Out("paused at:   %s (key %s)", s.CurrentStep.Name, s.CurrentStep.Key)
	}
	if e := deref(s.ExpiresAt); e != "" {
		ui.Out("expires:     %s", e)
	}
	if s.AttachAvailable {
		ui.Out("attach:      available")
	} else {
		// R1: never a silent "no" - the API always states the reason, and
		// the CLI is the surface where a remote-step pause has to say so.
		reason := deref(s.AttachUnavailableReason)
		if reason == "" {
			reason = "no reason given"
		}
		ui.Out("attach:      unavailable - %s", reason)
	}
	if r := deref(s.EndReason); r != "" {
		ui.Out("ended:       %s", r)
	}
}
