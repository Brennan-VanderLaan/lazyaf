package cmd

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/gitx"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// `lazyaf ingest REPO_PATH --name NAME [--branch B | --all-branches]`.
// Port of cli.py ingest (:500-660).
func newIngestCmd(d *Deps) *cobra.Command {
	var (
		name        string
		branch      string
		allBranches bool
	)
	cmd := &cobra.Command{
		Use:   "ingest REPO_PATH",
		Short: "Ingest a local git repository into LazyAF",
		Long: "Ingest a local git repository into LazyAF.\n\n" +
			"This creates a repo record and pushes the content to LazyAF's internal git server.\n" +
			"Agents will work against this internal copy, keeping your real remote clean.",
		Example: "  lazyaf ingest ./my-project --name my-project\n" +
			"  lazyaf ingest ./my-project --name my-project --branch main\n" +
			"  lazyaf ingest ./my-project --name my-project --all-branches",
		Args: exactArgs("REPO_PATH"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireFlag(cmd, "name", name); err != nil {
				return err
			}
			return runIngest(cmd.Context(), d, args[0], name, branch, allBranches)
		},
	}
	cmd.Flags().StringVarP(&name, "name", "n", "", "Name for the repo in LazyAF (required)")
	cmd.Flags().StringVarP(&branch, "branch", "b", "", "Branch to push (default: current branch)")
	cmd.Flags().BoolVarP(&allBranches, "all-branches", "a", false, "Push all branches")
	// A branch name is never a path; stop the shell offering files for it.
	_ = cmd.RegisterFlagCompletionFunc("branch", cobra.NoFileCompletions)
	return cmd
}

func runIngest(ctx context.Context, d *Deps, repoPath, name, branch string, allBranches bool) error {
	// click.Path(exists=True, file_okay=False, resolve_path=True): a missing
	// or non-directory path is a usage error, and the path is made absolute
	// so every later message names one place.
	path, err := filepath.Abs(repoPath)
	if err != nil {
		path = repoPath
	}
	if st, err := os.Stat(path); err != nil || !st.IsDir() {
		return ui.Usage(fmt.Sprintf("Invalid value for 'REPO_PATH': directory '%s' does not exist.", repoPath),
			ui.WithRemedy("A working lazyaf ingest looks like:\n"+
				"  lazyaf ingest ./my-project --name my-project"))
	}

	// --name is required, so the flag check catches its absence. An
	// all-whitespace name gets past that and was rejected by the API's
	// min_length with a raw pydantic envelope; refusing here names the flag.
	name = strings.TrimSpace(name)
	if name == "" {
		suggested := filepath.Base(path)
		if suggested == "" || suggested == "." {
			suggested = "my-project"
		}
		return ui.Fail("--name is empty",
			ui.WithRemedy("The name is how the repo is listed in LazyAF and in "+
				"`lazyaf list`, so it cannot be blank:\n\n"+
				fmt.Sprintf("    lazyaf ingest %s --name %s", repoPath, suggested)))
	}

	if _, err := os.Stat(filepath.Join(path, ".git")); err != nil {
		return ui.Fail(fmt.Sprintf("%s is not a git repository", path),
			ui.WithRemedy("ingest takes the path to a git working tree, and pushes it "+
				"to LazyAF's internal git server.\n\n"+
				fmt.Sprintf("    cd %s && git init\n", path)+
				fmt.Sprintf("    lazyaf ingest %s --name %s", repoPath, name)))
	}

	if err := requirePushableContent(ctx, d.Git, path, branch); err != nil {
		return err
	}

	ui.Out("Ingesting %s from %s", name, path)

	// Detect the default branch if not specified. --all-branches used to skip
	// detection entirely and send `default_branch: "main"`, so a repo whose
	// trunk is `master` was ingested with every branch present and a default
	// naming none of them. Detect in BOTH modes; --all-branches changes what
	// gets PUSHED, not what the repo's default is (cli.py:540-563).
	if branch == "" {
		current, stderr, err := gitx.CurrentBranch(ctx, d.Git, path)
		if err != nil {
			fallback := "main"
			if names := gitx.LocalBranches(ctx, d.Git, path); len(names) > 0 {
				fallback = names[0]
			}
			return ui.Fail(fmt.Sprintf("could not detect the current branch of %s", path),
				ui.WithDetail(stderr),
				ui.WithRemedy("A detached HEAD has no branch name to push. Name one explicitly:\n\n"+
					fmt.Sprintf("    lazyaf ingest %s --name %s --branch %s", repoPath, name, fallback)))
		}
		branch = current
		ui.Out("Using current branch as the default: %s", branch)
	}

	remoteURL := gitx.RemoteURL(ctx, d.Git, path, "origin")

	client, err := newClient()
	if err != nil {
		return err
	}
	ui.Out("Creating repo on %s...", client.Base)
	body := api.RepoCreate{Name: name, DefaultBranch: branch}
	if remoteURL != "" {
		body.RemoteURL = &remoteURL
	}
	var created api.RepoIngest
	if err := client.Post(ctx, "/api/repos/ingest", body, &created); err != nil {
		return err
	}
	ui.Out("Created repo %s", created.ID)

	ui.Out("Adding lazyaf remote...")
	_, _ = gitx.Git(ctx, d.Git, path, "remote", "remove", "lazyaf") // remove if exists
	if _, err := gitx.GitOrFail(ctx, d.Git, path, []string{"remote", "add", "lazyaf", created.CloneURL},
		fmt.Sprintf("could not add the 'lazyaf' remote to %s", path),
		"The repo record exists on the server; only the local remote "+
			"failed. Add it by hand and push:\n\n"+
			fmt.Sprintf("    git -C %s remote add lazyaf %s\n", path, created.CloneURL)+
			fmt.Sprintf("    git -C %s push lazyaf %s", path, pushTarget(branch, allBranches)),
	); err != nil {
		return err
	}

	var pushArgs []string
	if allBranches {
		ui.Out("Pushing all branches...")
		pushArgs = []string{"push", "lazyaf", "--all"}
	} else {
		ui.Out("Pushing branch %s...", branch)
		pushArgs = []string{"push", "lazyaf", branch}
	}
	if _, err := gitx.GitOrFail(ctx, d.Git, path, pushArgs,
		fmt.Sprintf("could not push %s to LazyAF (repo %s was created)", path, created.ID),
		"git's reason is quoted above. The repo record exists but has no "+
			"content, so agents cannot branch from it. Fix the cause and "+
			"push again:\n\n"+
			fmt.Sprintf("    git -C %s push lazyaf %s", path, strings.Join(pushArgs[2:], " ")),
	); err != nil {
		return err
	}

	// A push can succeed and transfer nothing. Ask the server what it
	// actually has rather than reporting success on our own say-so.
	var landed api.Branches
	if err := client.Get(ctx, "/api/repos/"+created.ID+"/branches", &landed,
		api.NotFound(fmt.Sprintf("repo %s vanished between creating it and pushing to it", created.ID))); err != nil {
		return err
	}
	if len(landed.Branches) == 0 {
		return ui.Fail(fmt.Sprintf("the push reported success but repo %s still has no branches on the server", created.ID),
			ui.WithRemedy("Nothing was transferred, so an agent would have nothing to "+
				"check out. Push again and read git's output:\n\n"+
				fmt.Sprintf("    git -C %s push lazyaf %s", path, strings.Join(pushArgs[2:], " "))))
	}
	names := make([]string, 0, len(landed.Branches))
	for _, b := range landed.Branches {
		names = append(names, b.Name)
	}

	ui.Out("")
	ui.Out("Success!")
	ui.Out("")
	ui.Out("Repo ID: %s", created.ID)
	ui.Out("Server:  %s", client.Base)
	ui.Out("Clone URL: %s", created.CloneURL)
	ui.Out("Branches on the server: %s", strings.Join(names, ", "))
	ui.Out("")
	ui.Out("Create cards in the UI to start working with AI agents.")
	ui.Out("Check it from here with:  lazyaf branches %s", created.ID)
	return nil
}

func pushTarget(branch string, all bool) string {
	if all {
		return "--all"
	}
	return branch
}

// requirePushableContent refuses to ingest a repo that has nothing to push.
// R1, earliest point (cli.py:462-497): `git push --all` on a repo with no
// commits succeeds and transfers NOTHING, so ingest used to print a green
// "Success!" over an empty repo. A named branch that does not exist is
// refused here too, BEFORE the API call: pushing it fails anyway, but only
// after a repo record has been created, leaving an empty repo in LazyAF
// for every typo.
func requirePushableContent(ctx context.Context, git gitx.Runner, path, branch string) error {
	branches := gitx.LocalBranches(ctx, git, path)
	if len(branches) == 0 {
		return ui.Fail(fmt.Sprintf("%s is a git repository with no commits, so there is nothing to ingest", path),
			ui.WithRemedy("LazyAF stores your code on its own git server and agents "+
				"branch from what you push. An empty repo gives them nothing "+
				"to check out.\n\n"+
				"Commit something first:\n"+
				"    git add -A\n"+
				"    git commit -m \"initial commit\"\n\n"+
				"Then run this command again."))
	}
	if branch != "" {
		found := false
		for _, b := range branches {
			if b == branch {
				found = true
			}
		}
		if !found {
			sorted := append([]string(nil), branches...)
			sort.Strings(sorted)
			return ui.Fail(fmt.Sprintf("branch '%s' does not exist in %s", branch, path),
				ui.WithRemedy("Local branches:\n    "+strings.Join(sorted, "\n    ")+
					"\n\nPick one of those, or drop --branch to push the current branch."))
		}
	}
	return nil
}
