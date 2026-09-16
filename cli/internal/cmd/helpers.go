package cmd

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// newClient resolves the backend from the persistent --server flag.
// serverSetting is the persistent --server flag with its provenance.
//
// Explicit is true when the flag was passed - recorded by the root's
// PersistentPreRunE - OR when it carries a value. The second half is not
// redundant: cobra's hidden `__complete` command parses flags but runs no
// PreRun hooks, so during completion ServerExplicit is never set and a
// `--server URL` typed on the line would otherwise be ignored in favour of
// $LAZYAF_SERVER (TestDynamicCompletion/the_flag_on_the_line_wins). Value
// alone was the whole rule before; this keeps it and adds the one case it
// could not express, `--server ""`, which must refuse rather than fall
// through (V4-2, R1).
func serverSetting() api.ServerSetting {
	return api.ServerSetting{Value: ServerFlag, Explicit: ServerExplicit || ServerFlag != ""}
}

func newClient() (*api.Client, error) { return api.NewFromSetting(serverSetting()) }

// exactArgs is cobra.ExactArgs with click's wording and the command's
// Example attached: "Missing argument 'REPO_ID'" and a working line, not
// "accepts 1 arg(s), received 0" (cli.py:389-410).
func exactArgs(names ...string) cobra.PositionalArgs {
	return func(cmd *cobra.Command, args []string) error {
		if len(args) < len(names) {
			return ui.Usage(fmt.Sprintf("Missing argument '%s'.", names[len(args)]),
				ui.WithRemedy(usageRemedy(cmd)))
		}
		if len(args) > len(names) {
			return ui.Usage(fmt.Sprintf("Got unexpected extra argument (%s).", strings.Join(args[len(names):], " ")),
				ui.WithRemedy(usageRemedy(cmd)))
		}
		return nil
	}
}

// requireFlag is the check a required option gets in RunE rather than via
// cobra.MarkFlagRequired: cobra's `required flag(s) "name" not set` names
// the flag without its dashes, and the owner's report was exactly that the
// missing flag was not shown as something to type (test_cli_errors.py:294).
func requireFlag(cmd *cobra.Command, name, value string) error {
	if strings.TrimSpace(value) != "" || cmd.Flags().Changed(name) {
		return nil
	}
	return ui.Usage(fmt.Sprintf("Missing option '--%s'.", name), ui.WithRemedy(usageRemedy(cmd)))
}

// newGroup is a command that only holds subcommands. cobra treats a bare or
// mistyped group as a request for help and exits 0; a script that ran
// `lazyaf debug resmue <id>` would read that as success. So a group refuses
// (exit 2) with the same suggestion mechanism the root has
// (LazyafGroup.resolve_command, cli.py:420-428).
func newGroup(use, short, long, example string) *cobra.Command {
	g := &cobra.Command{
		Use:     use,
		Short:   short,
		Long:    long,
		Example: example,
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) > 0 {
				return unknownSubcommand(cmd, args[0])
			}
			_ = cmd.Help()
			return ui.Usage(cmd.CommandPath()+" needs a subcommand", ui.WithRemedy(usageRemedy(cmd)))
		},
	}
	return g
}

// unknownSubcommand is cobra's own "unknown command" sentence with its
// Levenshtein suggestion, as a usage refusal that carries the group's
// Example. cobra only produces this for the ROOT (legacyArgs, args.go:28);
// nested groups need it spelled out.
func unknownSubcommand(cmd *cobra.Command, name string) error {
	msg := fmt.Sprintf("unknown command %q for %q", name, cmd.CommandPath())
	// cobra defaults the distance to 2 only inside its private
	// findSuggestions (command.go:851); SuggestionsFor alone would use 0.
	if cmd.SuggestionsMinimumDistance <= 0 {
		cmd.SuggestionsMinimumDistance = 2
	}
	if s := cmd.SuggestionsFor(name); len(s) > 0 {
		msg += "\n\nDid you mean this?\n\t" + strings.Join(s, "\n\t")
	}
	return ui.Usage(msg, ui.WithRemedy(usageRemedy(cmd)))
}

// joinOrNone renders a list the way the Python CLI did: comma-joined, or
// "(none)" when empty (cli.py:1414-1418).
func joinOrNone(items []string) string {
	if len(items) == 0 {
		return "(none)"
	}
	return strings.Join(items, ", ")
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
