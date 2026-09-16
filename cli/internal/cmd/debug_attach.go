package cmd

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/debugproto"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/terminal"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// Banner is what the operator is told before the shell opens (debug_cmd.py
// BANNER, :62-65). On stderr, because stdout is the shell's own byte stream
// once attached (§3.6).
const Banner = "/workspace is mounted READ-WRITE: edits there are seen by the resumed step."

// NotATTY is the stated warning for the line-buffered path (debug_cmd.py
// :587-595): a pipe cannot be a raw TTY, and a full-screen program will
// misbehave in ways the operator has to know about up front.
const NotATTY = "stdin is not a TTY - keystrokes are sent a line at a time and full-screen programs will not render correctly."

// `lazyaf debug attach SESSION_ID [--sidecar|--shell] [--print-credential]
// [--token T]`. Port of debug_cmd.py attach (:521-624), plus --token (§5):
// the server's join_command already prints `--token <t>`
// (backend/app/routers/debug.py:119-121), and with this flag the client
// skips the mint so that line is literally true.
func newDebugAttachCmd(d *Deps) *cobra.Command {
	var (
		sidecar         bool
		shell           bool
		printCredential bool
		token           string
	)
	cmd := &cobra.Command{
		Use:   "attach SESSION_ID",
		Short: "Open an interactive shell on a paused session's sidecar",
		Long: "Open an interactive shell on a paused session's sidecar.\n\n" +
			"`--shell` is REFUSED, not downgraded: a breakpoint is a pre-step gate, so\n" +
			"the step container does not exist yet. Use the sidecar to inspect the\n" +
			"workspace the step is about to run against.\n\n" +
			"Ctrl-] then r/a/s/h drives @resume / @abort / @status / @help; every one\n" +
			"of those is also a plain `lazyaf debug` subcommand, so controlling a\n" +
			"session never depends on having a TTY. Ctrl-] then d detaches and leaves\n" +
			"the session paused.\n\n" +
			"--token uses a credential the server already minted (the join_command a\n" +
			"debug re-run prints) instead of minting a new one.",
		Example: "  lazyaf debug attach <session_id>\n" +
			"  lazyaf debug attach <session_id> --token <join_token>\n" +
			"  lazyaf debug attach <session_id> --print-credential",
		Args:              exactArgs("SESSION_ID"),
		ValidArgsFunction: completeSessionIDs,
		RunE: func(cmd *cobra.Command, args []string) error {
			if shell && cmd.Flags().Changed("sidecar") && sidecar {
				return ui.Usage("--sidecar and --shell are opposites; give one", ui.WithRemedy(usageRemedy(cmd)))
			}
			return runAttach(cmd.Context(), d, args[0], shell, printCredential, token)
		},
	}
	cmd.Flags().BoolVar(&sidecar, "sidecar", true, "Attach to the sidecar - the only mode at a breakpoint (default)")
	cmd.Flags().BoolVar(&shell, "shell", false, "Attach to the step container (refused at a pre-step breakpoint; see below)")
	cmd.Flags().BoolVar(&printCredential, "print-credential", false, "Mint and print the join credential instead of opening a shell")
	cmd.Flags().StringVar(&token, "token", "", "Use this join token instead of minting one (the join_command the server prints)")
	_ = cmd.RegisterFlagCompletionFunc("token", cobra.NoFileCompletions)
	return cmd
}

func runAttach(ctx context.Context, d *Deps, sessionID string, shell, printCredential bool, token string) error {
	if shell {
		// C17: refused with the SERVER's sentence word for word (corpus
		// constants.reasons; debugproto.ShellRefusedReason is pinned to it),
		// never a silent fall back to the sidecar.
		return ui.Usage(debugproto.ShellRefusedReason,
			ui.WithRemedy("Use the sidecar to inspect the workspace the step is about to run against:\n\n"+
				fmt.Sprintf("    lazyaf debug attach %s --sidecar", sessionID)))
	}

	client, err := newClient()
	if err != nil {
		return err
	}
	var session api.DebugSessionInfo
	if err := client.Get(ctx, "/api/debug/"+sessionID, &session,
		api.NotFound(fmt.Sprintf("debug session %s does not exist on the LazyAF backend. `lazyaf debug list` shows the ones that do.", sessionID))); err != nil {
		return err
	}
	if !session.AttachAvailable {
		// C16: a remote-step pause says why out loud rather than silently
		// attaching to the wrong volume.
		reason := deref(session.AttachUnavailableReason)
		if reason == "" {
			reason = "the server gave no reason"
		}
		return ui.Usage("cannot attach to this session: "+reason,
			ui.WithRemedy("A session is attachable only while it is paused at a "+
				"breakpoint on a local step.\n\n"+
				fmt.Sprintf("    lazyaf debug status %s", sessionID)))
	}

	expires := "(supplied with --token)"
	if token == "" {
		var minted api.DebugJoinTokenResponse
		if err := client.Post(ctx, "/api/debug/"+sessionID+"/join-token", nil, &minted); err != nil {
			return err
		}
		token, expires = minted.Token, minted.ExpiresAt
	}
	url, err := terminal.TerminalURL(client.Base, sessionID, debugproto.ConnectionModeSidecar)
	if err != nil { // unreachable after api.New validated the scheme; stated
		return ui.Fail(err.Error(), ui.WithRemedy(client.Describe()))
	}

	if printCredential {
		ui.Out("")
		ui.Out("token:   %s", token)
		ui.Out("expires: %s", expires)
		ui.Out("socket:  %s", url)
		ui.Out("")
		ui.Out("%s", Banner)
		return nil
	}

	ui.Note("connecting to %s", url)
	ui.Warn(Banner, "")

	console, restore, warning := d.Console()
	restored := false
	restoreOnce := func() {
		if !restored {
			restored = true
			restore()
		}
	}
	defer restoreOnce() // on every path, Ctrl-C and panic included (§12)
	if warning != "" {
		ui.Warn(warning, "")
	}
	ui.Note("console: %s", console.Mode())
	if console.Mode() == terminal.ModeLineBuffered {
		ui.Warn(NotATTY, "")
	}

	result, err := terminal.Attach(ctx, d.Dial, url, token, console, func(text string) { ui.Note("%s", text) }, sessionID)
	restoreOnce() // the outcome is printed on a sane terminal
	if err != nil {
		if ctx.Err() != nil {
			return err // Run reports the interrupt
		}
		return ui.Fail(fmt.Sprintf("terminal connection to %s failed", url),
			ui.WithDetail(err.Error()), ui.WithRemedy(client.Describe()))
	}
	if result.ExitCode != 0 {
		return ui.Fail(result.Reason, ui.WithExit(result.ExitCode))
	}
	ui.Note("")
	ui.Note("%s", result.Reason)
	return nil
}
