package cmd

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/gitx"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf land REPO_ID --branch B [--remote R] [--pr] [--base B]`.
// Port of cli.py land (:669-813).
func newLandCmd(d *Deps) *cobra.Command {
	var (
		branch string
		remote string
		pr     bool
		base   string
	)
	cmd := &cobra.Command{
		Use:   "land REPO_ID",
		Short: "Land a branch from LazyAF's internal git server to a real remote",
		Long: "Land a branch from LazyAF's internal git server to a real remote.\n\n" +
			"This fetches the branch from LazyAF and pushes it to your configured remote\n" +
			"(usually origin/GitHub/GitLab). Run it inside your clone.",
		Example: "  lazyaf land abc123 --branch feature/new-api\n" +
			"  lazyaf land abc123 --branch feature/new-api --pr\n" +
			"  lazyaf land abc123 --branch feature/new-api --pr --base develop",
		Args:              exactArgs("REPO_ID"),
		ValidArgsFunction: completeRepoIDs,
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireFlag(cmd, "branch", branch); err != nil {
				return err
			}
			return runLand(cmd.Context(), d, args[0], branch, remote, pr, base)
		},
	}
	cmd.Flags().StringVarP(&branch, "branch", "b", "", "Branch to land (required)")
	cmd.Flags().StringVarP(&remote, "remote", "r", "origin", "Remote to push to")
	cmd.Flags().BoolVar(&pr, "pr", false, "Create a pull request using the gh CLI")
	cmd.Flags().StringVar(&base, "base", "", "Base branch for the PR (default: the repo's default branch)")
	_ = cmd.RegisterFlagCompletionFunc("branch", completeLandBranch)
	_ = cmd.RegisterFlagCompletionFunc("remote", completeRemotes(d))
	_ = cmd.RegisterFlagCompletionFunc("base", cobra.NoFileCompletions)
	return cmd
}

func runLand(ctx context.Context, d *Deps, repoID, branch, remote string, pr bool, base string) error {
	client, err := newClient()
	if err != nil {
		return err
	}
	ui.Out("Landing branch %s from repo %s", branch, repoID)
	ui.Out("Fetching repo info from %s...", client.Base)

	notFound := api.NotFound(fmt.Sprintf("repo %s does not exist on the LazyAF backend. `lazyaf list` shows the ids that do.", repoID))
	var repo api.Repo
	if err := client.Get(ctx, "/api/repos/"+repoID, &repo, notFound); err != nil {
		return err
	}
	var clone api.CloneURL
	if err := client.Get(ctx, "/api/repos/"+repoID+"/clone-url", &clone, notFound); err != nil {
		return err
	}
	defaultBranch := repo.DefaultBranch
	if defaultBranch == "" {
		defaultBranch = "main"
	}
	baseBranch := base
	if baseBranch == "" {
		baseBranch = defaultBranch
	}
	if deref(repo.RemoteURL) == "" {
		ui.Warn(fmt.Sprintf("repo %s has no remote_url recorded, so LazyAF cannot confirm that '%s' is the right destination", repoID, remote), "")
	}

	// land pushes from YOUR clone to YOUR remote, so it has to run inside
	// that clone.
	cwd, err := os.Getwd()
	if err != nil {
		return ui.Fail("could not read the current directory", ui.WithDetail(err.Error()))
	}
	if _, err := os.Stat(filepath.Join(cwd, ".git")); err != nil {
		return ui.Fail(fmt.Sprintf("the current directory is not a git repository: %s", cwd),
			ui.WithRemedy("land pushes from YOUR clone to YOUR remote, so it has to run "+
				"inside that clone:\n\n"+
				"    cd /path/to/your/clone\n"+
				fmt.Sprintf("    lazyaf land %s --branch %s", repoID, branch)))
	}

	ui.Out("Configuring lazyaf remote...")
	_, _ = gitx.Git(ctx, d.Git, cwd, "remote", "remove", "lazyaf")
	if _, err := gitx.GitOrFail(ctx, d.Git, cwd, []string{"remote", "add", "lazyaf", clone.CloneURL},
		fmt.Sprintf("could not add the 'lazyaf' remote to %s", cwd),
		"    git remote add lazyaf "+clone.CloneURL); err != nil {
		return err
	}

	ui.Out("Fetching %s from LazyAF...", branch)
	if _, err := gitx.GitOrFail(ctx, d.Git, cwd, []string{"fetch", "lazyaf", branch},
		fmt.Sprintf("could not fetch branch '%s' from LazyAF", branch),
		"git's reason is quoted above. If the branch simply is not "+
			"there, list what the server has:\n\n"+
			"    lazyaf branches "+repoID); err != nil {
		return err
	}

	// BOTH SIDES FULLY QUALIFIED, and not cosmetically (cli.py:751-768).
	// `lazyaf/<branch>:<branch>` fails whenever the destination branch does
	// not exist yet - the normal case for landing an agent branch:
	//
	//     error: The destination you provided is not a full refname [...]
	//     Neither worked, so we gave up. You must fully qualify the ref.
	//
	// git cannot tell whether an unqualified <dst> means a branch or a tag
	// when nothing of that name is there to match. Naming refs/heads/ says
	// it. TestLand asserts this on the recorded argv.
	refspec := fmt.Sprintf("refs/remotes/lazyaf/%s:refs/heads/%s", branch, branch)
	ui.Out("Pushing to %s/%s...", remote, branch)
	if _, err := gitx.GitOrFail(ctx, d.Git, cwd, []string{"push", remote, refspec},
		fmt.Sprintf("could not push '%s' to remote '%s'", branch, remote),
		"git's reason is quoted above. Nothing was landed. Check the "+
			"remote exists and you can write to it:\n\n"+
			"    git remote -v\n"+
			fmt.Sprintf("    git push %s %s", remote, refspec)); err != nil {
		return err
	}
	ui.Out("Pushed branch %s to %s", branch, remote)

	if pr {
		ui.Out("")
		ui.Out("Creating PR against %s...", baseBranch)
		ghArgs := []string{"pr", "create", "--base", baseBranch, "--head", branch, "--fill"}
		res, err := d.Git.Run(ctx, cwd, "gh", ghArgs...)
		if err != nil || res.ExitCode != 0 {
			// The branch landed; the PR did not. Reporting that as success
			// (which is what a 0 exit meant here) tells a script the whole
			// request was honoured when half of it was not - so the branch
			// result is stated plainly and the exit code says "failed".
			detail := strings.TrimSpace(res.Stderr)
			if detail == "" {
				detail = strings.TrimSpace(res.Stdout)
			}
			if err != nil {
				detail = err.Error()
			}
			return ui.Fail(fmt.Sprintf("branch '%s' was pushed to %s, but --pr could not create the pull request", branch, remote),
				ui.WithDetail(detail),
				ui.WithRemedy("The push is done and does not need repeating. Create "+
					"the PR when the cause above is fixed:\n\n"+
					"    gh "+strings.Join(ghArgs, " ")))
		}
		ui.Out("Created PR: %s", strings.TrimSpace(res.Stdout))
	}

	ui.Out("")
	ui.Out("Landed!")
	ui.Out("")
	ui.Out("Branch %s is now on %s", branch, remote)
	return nil
}
