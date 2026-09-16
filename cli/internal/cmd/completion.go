package cmd

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/gitx"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// Shell completion (upcoming/go-cli.md §7).
//
// `lazyaf completion <shell>` prints cobra's generated script. Every script
// delegates to the hidden `__complete` command, so subcommands, flags and
// help strings complete from the command tree itself and cannot drift from
// it. Dynamic values - repo ids, session ids, branches - come from the
// backend on TAB.
//
// THE ONE DELIBERATELY QUIET PATH IN THE BINARY, and why. R1 says nothing
// fails dark; a completion callback is the structural exception: it runs
// while the shell is composing the command line, and anything it prints
// lands IN that line. So a remote lookup that fails returns
// ShellCompDirectiveError with zero candidates and says nothing. The cost
// is bounded: RemoteTimeout caps every lookup, so a dead $LAZYAF_SERVER
// costs one second per TAB and never hangs the shell, and
// LAZYAF_COMPLETE_NO_REMOTE=1 turns the remote lookups off entirely. A
// failure that matters is still loud where it belongs - the command the
// operator runs next, which names the URL and its provenance.
//
// Not completed, stated: `debug rerun RUN_ID` (no listing endpoint) and
// `--break` step keys (needs run -> pipeline -> graph lookups).

// NoRemoteEnvVar disables the backend lookups on TAB.
const NoRemoteEnvVar = "LAZYAF_COMPLETE_NO_REMOTE"

// RemoteTimeout bounds one completion lookup.
const RemoteTimeout = time.Second

// Shells are the four dialects cobra generates, in the order --help lists
// them.
var Shells = []string{"bash", "zsh", "fish", "powershell"}

func newCompletionCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "completion SHELL",
		Short: "Print the shell completion script for bash, zsh, fish or powershell",
		Long: "Print the shell completion script for bash, zsh, fish or powershell.\n\n" +
			"install.sh sets bash up for you; for the others, or by hand:\n\n" +
			"  bash:        source <(lazyaf completion bash)\n" +
			"               # or, with bash-completion 2.x installed, once:\n" +
			"               lazyaf completion bash > ${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions/lazyaf\n" +
			"  zsh:         lazyaf completion zsh > \"${fpath[1]}/_lazyaf\"   # then restart zsh\n" +
			"  fish:        lazyaf completion fish > ~/.config/fish/completions/lazyaf.fish\n" +
			"  powershell:  lazyaf completion powershell | Out-String | Invoke-Expression   # in $PROFILE\n\n" +
			"Repo and session ids complete from the backend (--server / $LAZYAF_SERVER),\n" +
			"with a " + RemoteTimeout.String() + " timeout per lookup; set " + NoRemoteEnvVar + "=1 to keep TAB local.",
		Example: "  lazyaf completion bash > ~/.local/share/bash-completion/completions/lazyaf\n" +
			"  source <(lazyaf completion bash)\n" +
			"  lazyaf completion powershell | Out-String | Invoke-Expression",
		ValidArgs:             Shells,
		DisableFlagsInUseLine: true,
		Args: func(cmd *cobra.Command, args []string) error {
			if len(args) != 1 {
				return ui.Usage("Missing argument 'SHELL'.", ui.WithRemedy(usageRemedy(cmd)))
			}
			for _, s := range Shells {
				if args[0] == s {
					return nil
				}
			}
			return ui.Usage(fmt.Sprintf("unknown shell %q; one of: %s", args[0], strings.Join(Shells, ", ")),
				ui.WithRemedy(usageRemedy(cmd)))
		},
		RunE: func(cmd *cobra.Command, args []string) error {
			root := cmd.Root()
			out := cmd.OutOrStdout()
			var err error
			switch args[0] {
			case "bash":
				err = root.GenBashCompletionV2(out, true)
			case "zsh":
				err = root.GenZshCompletion(out)
			case "fish":
				err = root.GenFishCompletion(out, true)
			case "powershell":
				err = root.GenPowerShellCompletionWithDesc(out)
			}
			if err != nil {
				return ui.Fail("could not write the completion script", ui.WithDetail(err.Error()))
			}
			return nil
		},
	}
}

// remoteLookup builds the bounded client one completion callback uses, or
// reports that lookups are off. cancel must be deferred by the caller.
func remoteLookup() (*api.Client, context.Context, context.CancelFunc, bool) {
	if v := os.Getenv(NoRemoteEnvVar); v != "" && v != "0" {
		return nil, nil, nil, false
	}
	client, err := api.NewFromSetting(serverSetting())
	if err != nil {
		return nil, nil, nil, false
	}
	client.HTTP.Timeout = RemoteTimeout
	ctx, cancel := context.WithTimeout(context.Background(), RemoteTimeout)
	return client, ctx, cancel, true
}

// completeRepoIDs: GET /api/repos, shown as `id<TAB>name`.
func completeRepoIDs(cmd *cobra.Command, args []string, toComplete string) ([]cobra.Completion, cobra.ShellCompDirective) {
	if len(args) > 0 {
		return nil, cobra.ShellCompDirectiveNoFileComp
	}
	client, ctx, cancel, ok := remoteLookup()
	if !ok {
		return nil, cobra.ShellCompDirectiveNoFileComp
	}
	defer cancel()
	var repos []api.Repo
	if err := client.Get(ctx, "/api/repos", &repos); err != nil {
		return nil, cobra.ShellCompDirectiveError
	}
	var out []cobra.Completion
	for _, r := range repos {
		if strings.HasPrefix(r.ID, toComplete) {
			out = append(out, cobra.CompletionWithDesc(r.ID, r.Name))
		}
	}
	return out, cobra.ShellCompDirectiveNoFileComp
}

// completeSessionIDs: GET /api/debug, shown as `id<TAB>status at step`.
func completeSessionIDs(cmd *cobra.Command, args []string, toComplete string) ([]cobra.Completion, cobra.ShellCompDirective) {
	if len(args) > 0 {
		return nil, cobra.ShellCompDirectiveNoFileComp
	}
	client, ctx, cancel, ok := remoteLookup()
	if !ok {
		return nil, cobra.ShellCompDirectiveNoFileComp
	}
	defer cancel()
	var sessions []api.DebugSessionInfo
	if err := client.Get(ctx, "/api/debug", &sessions); err != nil {
		return nil, cobra.ShellCompDirectiveError
	}
	var out []cobra.Completion
	for _, s := range sessions {
		if !strings.HasPrefix(s.ID, toComplete) {
			continue
		}
		desc := s.Status
		if s.CurrentStep != nil {
			desc += " at " + s.CurrentStep.Key
		}
		out = append(out, cobra.CompletionWithDesc(s.ID, desc))
	}
	return out, cobra.ShellCompDirectiveNoFileComp
}

// completeLandBranch: `--branch` on land from GET /api/repos/{id}/branches
// when the id is already on the line; nothing (quietly) when it is not.
func completeLandBranch(cmd *cobra.Command, args []string, toComplete string) ([]cobra.Completion, cobra.ShellCompDirective) {
	if len(args) == 0 {
		return nil, cobra.ShellCompDirectiveNoFileComp
	}
	client, ctx, cancel, ok := remoteLookup()
	if !ok {
		return nil, cobra.ShellCompDirectiveNoFileComp
	}
	defer cancel()
	var data api.Branches
	if err := client.Get(ctx, "/api/repos/"+args[0]+"/branches", &data); err != nil {
		return nil, cobra.ShellCompDirectiveError
	}
	var out []cobra.Completion
	for _, b := range data.Branches {
		if strings.HasPrefix(b.Name, toComplete) {
			out = append(out, b.Name)
		}
	}
	return out, cobra.ShellCompDirectiveNoFileComp
}

// completeRemotes: `--remote` from `git remote` in the current directory.
// Local, so it ignores LAZYAF_COMPLETE_NO_REMOTE; still bounded by
// RemoteTimeout because git on a network filesystem can stall too.
func completeRemotes(d *Deps) cobra.CompletionFunc {
	return func(cmd *cobra.Command, args []string, toComplete string) ([]cobra.Completion, cobra.ShellCompDirective) {
		ctx, cancel := context.WithTimeout(context.Background(), RemoteTimeout)
		defer cancel()
		cwd, err := os.Getwd()
		if err != nil {
			return nil, cobra.ShellCompDirectiveError
		}
		var out []cobra.Completion
		for _, r := range gitx.Remotes(ctx, d.Git, cwd) {
			if strings.HasPrefix(r, toComplete) {
				out = append(out, r)
			}
		}
		return out, cobra.ShellCompDirectiveNoFileComp
	}
}
