// Package cmd is the cobra command tree.
//
// root.go was written by L2 so the module builds and `--version` works
// (upcoming/go-cli.md §13.1); L3 extends it at the registration seam below
// and owns every other file in this package. Resolution of --server is
// api.ResolveServerURL's: flag > $LAZYAF_SERVER > http://localhost:8000,
// refused rather than guessed when schemeless (§5).
//
// THE USAGE-ERROR CONTRACT (port of LazyafCommand.parse_args, cli.py:389-410,
// and LazyafGroup.resolve_command, :420-428): click named the missing
// parameter and then pointed at `--help`; naming the flag is not the same as
// showing the command that works. Every command here has an Example, and
// every usage error - a missing flag, a missing positional, an unknown
// subcommand - reprints it. A mistyped subcommand gets cobra's Levenshtein
// suggestion; a wholly unknown one gets none.
package cmd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"strings"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/gitx"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/terminal"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/version"
)

// ServerFlag is the value of the persistent --server/-s flag ("" when
// unset, so api.ResolveServerURL falls through to the environment).
var ServerFlag string

// ServerExplicit is true when --server/-s was PASSED, even as "". cobra
// cannot tell `--server ""` from "not given" by value alone, and the value
// alone is what api.New used - so an explicit empty URL fell through to
// $LAZYAF_SERVER and then to the default, silently (V4-2, R1). The
// persistent pre-run below records Flags().Changed("server") here and every
// client is built from the pair via api.NewFromSetting.
var ServerExplicit bool

var (
	showVersion  bool
	shortVersion bool
)

// Deps are the process seams the commands reach the world through: git and
// gh (gitx.Runner), the terminal socket (terminal.Dialer) and the local
// console. Tests hand in recorders and fakes; the HTTP client is NOT here
// because internal/api owns it (api.TestOnlyAPIImportsNetHTTP) and a test
// points --server at an httptest.Server instead.
type Deps struct {
	Git  gitx.Runner
	Dial terminal.Dialer
	// Console opens the operator's console for `debug attach`: the io, the
	// restore func the command defers, and a non-empty warning when a tty
	// could not enter raw mode (terminal.NewConsole).
	Console func() (terminal.ConsoleIO, func(), string)
}

// DefaultDeps are the real seams.
func DefaultDeps() *Deps {
	return &Deps{
		Git:  gitx.Exec{},
		Dial: terminal.Dial,
		Console: func() (terminal.ConsoleIO, func(), string) {
			return terminal.NewConsole(os.Stdin, os.Stdout)
		},
	}
}

// NewRoot builds the command tree over the real seams. A fresh tree per
// call, so tests can execute it with their own args and streams without
// sharing state.
func NewRoot() *cobra.Command { return NewRootWith(DefaultDeps()) }

// NewRootWith builds the command tree over the given seams.
func NewRootWith(d *Deps) *cobra.Command {
	root := &cobra.Command{
		Use:   "lazyaf",
		Short: "LazyAF - Visual orchestrator for AI agents",
		Long: "LazyAF - Visual orchestrator for AI agents.\n\n" +
			"Every command talks to a LazyAF backend, named by --server or\n" +
			"$LAZYAF_SERVER and defaulting to http://localhost:8000.",
		Example:       "  lazyaf list --server http://localhost:8000\n  lazyaf --version",
		SilenceErrors: true, // ui.Report is the one printer of refusals
		SilenceUsage:  true, // a usage error reprints the Example instead (Run)
		RunE: func(cmd *cobra.Command, args []string) error {
			if showVersion {
				if shortVersion {
					fmt.Fprintln(cmd.OutOrStdout(), version.Describe())
				} else {
					fmt.Fprintln(cmd.OutOrStdout(), version.Line())
				}
				return nil
			}
			if shortVersion {
				return ui.Usage("--short only means something with --version",
					ui.WithRemedy("    lazyaf --version --short"))
			}
			return cmd.Help()
		},
	}
	// Runs before every subcommand (cobra chains PersistentPreRunE from the
	// root). Its only job is the provenance bit above; it must not resolve the
	// URL itself, because --version and --help must never touch the network.
	root.PersistentPreRunE = func(cmd *cobra.Command, _ []string) error {
		ServerExplicit = cmd.Flags().Changed("server")
		return nil
	}
	root.PersistentFlags().StringVarP(&ServerFlag, "server", "s", "",
		"LazyAF server URL (default: $LAZYAF_SERVER, else http://localhost:8000)")
	root.Flags().BoolVar(&showVersion, "version", false, "print the version and exit")
	root.Flags().BoolVar(&shortVersion, "short", false, "with --version: print only the version token")
	root.SetOut(ui.Stdout)
	root.SetErr(ui.Stderr)
	// A flag cobra cannot parse is a usage error (exit 2), like click's.
	root.SetFlagErrorFunc(func(cmd *cobra.Command, err error) error {
		return ui.Usage(err.Error(), ui.WithRemedy(usageRemedy(cmd)))
	})
	// cobra's own `completion` command is replaced by completion.go's, which
	// carries the Example and the four one-liners install.sh prints (§7).
	root.CompletionOptions.DisableDefaultCmd = true

	root.AddCommand(
		newIngestCmd(d),
		newLandCmd(d),
		newListCmd(),
		newBranchesCmd(),
		newTestsCmd(),
		newDebugCmd(d),
		newInitCmd(),
		newDoctorCmd(),
		newCompletionCmd(),
	)
	// cobra's `help` subcommand exists whether or not we add it; giving it an
	// Example here keeps "every command documents a runnable example"
	// true of the whole tree, not of the tree minus one.
	root.InitDefaultHelpCmd()
	for _, c := range root.Commands() {
		if c.Name() == "help" && c.Example == "" {
			c.Example = "  lazyaf help ingest\n  lazyaf help debug attach"
		}
	}

	return root
}

// usageRemedy is the working example a usage error reprints (the port of
// LazyafCommand.parse_args, cli.py:389-410): the command's own Example, so
// the line a reader gets after a mistake is the one --help shows.
func usageRemedy(cmd *cobra.Command) string {
	if cmd == nil {
		return "    lazyaf --help"
	}
	if cmd.Example == "" {
		return "    " + cmd.CommandPath() + " --help"
	}
	return "A working " + cmd.CommandPath() + " looks like:\n" + cmd.Example
}

// Execute runs the tree against os.Args and returns the process exit code:
// 0 ok / 1 failure / 2 usage / 130 interrupt (§5). main.go is
// os.Exit(cmd.Execute()) and nothing else.
func Execute() int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	return Run(ctx, NewRoot())
}

// Run executes one built tree and maps its outcome to an exit code. It is
// the one place cobra's own errors become refusals: a *ui.Failure is
// reported as itself; any other error cobra hands back is, by construction,
// a usage error (unknown command, bad positional count, unknown flag -
// every RunE in this package returns *ui.Failure for a runtime problem), so
// it is reprinted with the failing command's Example and exits 2. Exported
// so a test in another package drives the production path, not a copy.
func Run(ctx context.Context, root *cobra.Command) int {
	c, err := root.ExecuteContextC(ctx)
	if err == nil {
		return ui.ExitOK
	}
	if !isFailure(err) {
		if ctx.Err() != nil {
			return ui.Report(ui.Interrupted())
		}
		// cobra's suggestion text ends in a newline; Report adds its own.
		err = ui.Usage(strings.TrimRight(err.Error(), "\n"), ui.WithRemedy(usageRemedy(c)))
	}
	return ui.Report(err)
}

func isFailure(err error) bool {
	var f *ui.Failure
	return errors.As(err, &f)
}
