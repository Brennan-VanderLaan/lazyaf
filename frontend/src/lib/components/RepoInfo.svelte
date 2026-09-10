<script lang="ts">
  import { onMount } from 'svelte';
  import { branchesStore, selectedRepo } from '../stores/repos';
  import { repos } from '../api/client';
  import type { BranchInfo, Commit } from '../api/types';
  import BranchManager from './BranchManager.svelte';
  import { formatRelative, fromEpochSeconds } from '../utils/time';

  let showBranchManager = false;

  let cloneUrl = '';
  let cloneUrlError = '';
  let commits: Commit[] = [];
  let loadingCommits = false;
  let copied = false;
  let selectedBranch: string | null = null;

  /**
   * The branch list is NOT local state. It lives in `branchesStore`, which is
   * snapshot-then-delta: this component's load() is the snapshot, and the
   * `repo_refs_changed` websocket frame is the delta. That is what makes a
   * push show up here without a reload - the defect this panel was reported
   * for. Keeping a private copy would reintroduce it.
   */
  $: branches = $branchesStore.repoId === $selectedRepo?.id ? $branchesStore.branches : [];
  $: branchesLoading = $branchesStore.repoId === $selectedRepo?.id && $branchesStore.loading;
  $: branchesError = $branchesStore.repoId === $selectedRepo?.id ? $branchesStore.error : null;
  $: branchesLoaded = $branchesStore.repoId === $selectedRepo?.id && $branchesStore.loaded;
  $: agentBranchCount = branches.filter(b => b.is_lazyaf).length;

  // Collapsible state - persisted to localStorage
  let isCollapsed = false;

  onMount(() => {
    const saved = localStorage.getItem('repoinfo-collapsed');
    if (saved !== null) {
      isCollapsed = saved === 'true';
    }
  });

  function toggleCollapsed() {
    isCollapsed = !isCollapsed;
    localStorage.setItem('repoinfo-collapsed', String(isCollapsed));
  }

  $: if ($selectedRepo) {
    loadRepoDetails();
  }

  $: if ($selectedRepo && selectedBranch) {
    loadCommits(selectedBranch);
  }

  async function loadRepoDetails() {
    if (!$selectedRepo) return;
    const repoId = $selectedRepo.id;

    try {
      const urlResponse = await repos.cloneUrl(repoId);
      cloneUrl = urlResponse.clone_url;
      cloneUrlError = '';
    } catch (e) {
      // Say so. An empty URL silently rendered as "..." forever, so the
      // copy buttons below handed out `git remote add lazyaf ` (R1).
      cloneUrl = '';
      cloneUrlError = e instanceof Error ? e.message : 'Failed to load the clone URL';
    }

    if ($selectedRepo.is_ingested) {
      await branchesStore.load(repoId);
    } else {
      branchesStore.clear();
      commits = [];
    }
  }

  /**
   * Keep a branch selected for the commit history below.
   *
   * Runs off the STORE, not off the fetch, so it settles the same way whether
   * the list arrived by fetch or by a live `repo_refs_changed` frame. A branch
   * that disappears from under the selection (deleted, or cleaned up) falls
   * back to the default rather than leaving the history pinned to a ref that
   * no longer exists.
   */
  $: if (branches.length === 0) {
    selectedBranch = null;
  } else if (!selectedBranch || !branches.some(b => b.name === selectedBranch)) {
    selectedBranch = (branches.find(b => b.is_default) ?? branches[0]).name;
  }

  function selectBranch(name: string) {
    selectedBranch = name;
  }

  function shortSha(sha: string | null | undefined): string {
    return sha ? sha.slice(0, 7) : '';
  }

  function retryBranches() {
    if ($selectedRepo) branchesStore.load($selectedRepo.id);
  }

  async function loadCommits(branch: string) {
    if (!$selectedRepo) return;
    loadingCommits = true;
    try {
      const response = await repos.commits($selectedRepo.id, branch, 15);
      commits = response.commits;
    } catch (e) {
      commits = [];
    } finally {
      loadingCommits = false;
    }
  }

  async function copyToClipboard(text: string) {
    await navigator.clipboard.writeText(text);
    copied = true;
    setTimeout(() => copied = false, 2000);
  }

  /** Commit times are unix SECONDS. Shared formatter - see utils/time.ts. */
  function formatTimestamp(timestamp: number): string {
    return formatRelative(fromEpochSeconds(timestamp));
  }

  function getBranchesAtCommit(sha: string): BranchInfo[] {
    return branches.filter(b => b.commit === sha);
  }
</script>

{#if $selectedRepo}
  <div class="repo-info-panel" class:collapsed={isCollapsed}>
    <button class="info-header" on:click={toggleCollapsed} type="button">
      <div class="header-left">
        <span class="collapse-icon">{isCollapsed ? '▶' : '▼'}</span>
        <h3>Repository Details</h3>
      </div>
      <span class="status-badge" class:ingested={$selectedRepo.is_ingested}>
        {$selectedRepo.is_ingested ? 'Ready' : 'Not Ingested'}
      </span>
    </button>

    {#if !isCollapsed && $selectedRepo.is_ingested}
      <div class="info-section">
        <label>Internal Git URL</label>
        <div class="url-box">
          <code>{cloneUrl || (cloneUrlError ? 'unavailable' : '...')}</code>
          <button
            class="btn-copy"
            on:click={() => copyToClipboard(cloneUrl)}
            disabled={!cloneUrl}
            title="Copy URL"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
        {#if cloneUrlError}
          <p class="failure" data-testid="clone-url-error">
            Could not read this repo's git URL: {cloneUrlError}
          </p>
        {/if}
      </div>

      <!--
        THE BRANCH LIST. Owner's words: "I need to be able to see which
        branches are available in lazyaf".

        This was a <select>, which shows exactly ONE branch until you open a
        native popup, and showed no commit at all. Every fact he asked for is
        now on the row: the name, which one is default, which came from an
        agent (the `lazyaf/` prefix), and the tip commit.
      -->
      <div class="info-section" data-testid="branches-section">
        <div class="section-header">
          <label>
            Branches{#if branchesLoaded && !branchesError}
              ({branches.length}{#if agentBranchCount > 0}, {agentBranchCount} from agents{/if})
            {/if}
          </label>
          {#if branches.length > 0}
            <button type="button" class="btn-manage" on:click={() => showBranchManager = true} title="Manage branches">
              Manage
            </button>
          {/if}
        </div>

        {#if branchesError}
          <!--
            An unreadable listing is NOT an empty repo. Conflating them was the
            reported defect's second half: a failed fetch rendered as "No
            branches yet. Push your repo to get started.", which told the user
            to redo a push that had already worked (R1).
          -->
          <div class="branch-failure" data-testid="branches-error">
            <p class="failure">Could not list branches: {branchesError}</p>
            <p class="muted">
              The repo and its commits are untouched — this is the listing that
              failed. Retry, and if it keeps failing use Manage → Sync from disk.
            </p>
            <button type="button" class="btn-retry" on:click={retryBranches}>Retry</button>
          </div>
        {:else if branchesLoading}
          <p class="muted">Loading branches...</p>
        {:else if branches.length > 0}
          <ul class="branch-list" data-testid="branch-list">
            {#each branches as branch (branch.name)}
              <li>
                <button
                  type="button"
                  class="branch-item"
                  class:lazyaf={branch.is_lazyaf}
                  class:selected={branch.name === selectedBranch}
                  data-testid="branch-row"
                  data-branch={branch.name}
                  on:click={() => selectBranch(branch.name)}
                  title={`Show commits on ${branch.name}`}
                >
                  <span class="branch-name">
                    <span class="branch-label">{branch.name}</span>
                    {#if branch.is_default}
                      <span class="badge default" data-testid="branch-badge-default">default</span>
                    {/if}
                    {#if branch.is_lazyaf}
                      <span class="badge lazyaf" data-testid="branch-badge-agent">agent</span>
                    {/if}
                  </span>
                  <code class="commit">{shortSha(branch.commit)}</code>
                </button>
              </li>
            {/each}
          </ul>
        {:else}
          <!--
            The empty state a new repo lands in. It has to read as a step not
            yet taken, and name the remedy - the owner hit this one straight
            after creating a repo. The commands it points at are the copyable
            ones in "Push Updates" below (R3: not duplicated here).
          -->
          <div class="branch-empty" data-testid="branches-empty">
            <p>No branches here yet.</p>
            <p class="muted">
              LazyAF is hosting this repo but nothing has been pushed into it.
              Run the two commands under <strong>Push Updates</strong> below —
              this list fills in the moment the push lands, no reload needed.
            </p>
          </div>
        {/if}
      </div>

      {#if branches.length > 0}
        <div class="info-section">
          <label>Commit History</label>
          {#if loadingCommits}
            <p class="muted">Loading commits...</p>
          {:else if commits.length > 0}
            <div class="git-graph">
              {#each commits as commit, i}
                {@const branchesHere = getBranchesAtCommit(commit.sha)}
                <div class="commit-row">
                  <div class="graph-line">
                    <div class="node" class:head={i === 0}></div>
                    {#if i < commits.length - 1}
                      <div class="connector"></div>
                    {/if}
                  </div>
                  <div class="commit-info">
                    <div class="commit-header">
                      <code class="commit-sha">{commit.short_sha}</code>
                      {#each branchesHere as branch}
                        <span class="branch-tag" class:default={branch.is_default} class:lazyaf={branch.is_lazyaf}>
                          {branch.name}
                        </span>
                      {/each}
                      <span class="commit-time">{formatTimestamp(commit.timestamp)}</span>
                    </div>
                    <div class="commit-message" title={commit.message}>
                      {commit.message.split('\n')[0]}
                    </div>
                    <div class="commit-author">{commit.author}</div>
                  </div>
                </div>
              {/each}
            </div>
          {:else}
            <p class="muted">No commits found.</p>
          {/if}
        </div>
      {/if}

      <div class="info-section">
        <label>Push Updates</label>
        <div class="instructions">
          <div class="cmd-group">
            <code>git remote add lazyaf {cloneUrl}</code>
            <button class="btn-copy-sm" on:click={() => copyToClipboard(`git remote add lazyaf ${cloneUrl}`)} title="Copy">
              Copy
            </button>
          </div>
          <div class="cmd-group">
            <code>git push lazyaf {$selectedRepo.default_branch}</code>
            <button class="btn-copy-sm" on:click={() => copyToClipboard(`git push lazyaf ${$selectedRepo.default_branch}`)} title="Copy">
              Copy
            </button>
          </div>
        </div>
      </div>

      <div class="info-section">
        <label>Pull Agent Work</label>
        <div class="instructions">
          <p class="instruction-note">Fetch and merge agent changes to your local repo:</p>
          <div class="cmd-group">
            <code>git fetch lazyaf</code>
            <button class="btn-copy-sm" on:click={() => copyToClipboard('git fetch lazyaf')} title="Copy">
              Copy
            </button>
          </div>
          {#if selectedBranch}
            <div class="cmd-group">
              <code>git merge lazyaf/{selectedBranch}</code>
              <button class="btn-copy-sm" on:click={() => copyToClipboard(`git merge lazyaf/${selectedBranch}`)} title="Copy">
                Copy
              </button>
            </div>
          {/if}
          <!--
            The label belongs INSIDE the guard with the command it introduces.
            Outside it, a repo with no branches yet - exactly a new user's
            state - rendered "Or checkout the branch directly:" immediately
            followed by "Then push to GitHub:", which reads as a command that
            failed to load. Its sibling `git merge` block above was already
            guarded correctly; this one was the straggler.
          -->
          {#if selectedBranch}
            <p class="instruction-note">Or checkout the branch directly:</p>
            <div class="cmd-group">
              <code>git checkout -b {selectedBranch} lazyaf/{selectedBranch}</code>
              <button class="btn-copy-sm" on:click={() => copyToClipboard(`git checkout -b ${selectedBranch} lazyaf/${selectedBranch}`)} title="Copy">
                Copy
              </button>
            </div>
          {/if}
          <p class="instruction-note">Then push to GitHub:</p>
          <div class="cmd-group">
            <code>git push origin {selectedBranch || $selectedRepo.default_branch}</code>
            <button class="btn-copy-sm" on:click={() => copyToClipboard(`git push origin ${selectedBranch || $selectedRepo.default_branch}`)} title="Copy">
              Copy
            </button>
          </div>
        </div>
      </div>
    {:else if !isCollapsed}
      <div class="info-section warning">
        <p>This repo needs to be ingested before agents can work on it.</p>
        <div class="instructions">
          <p><strong>From your repo directory, run:</strong></p>
          <code>lazyaf ingest</code>
          <p class="muted">Or use the API to ingest the repo first.</p>
        </div>
      </div>
    {/if}

    {#if !isCollapsed && $selectedRepo.remote_url}
      <div class="info-section">
        <label>Remote Origin</label>
        <code class="remote-url">{$selectedRepo.remote_url}</code>
      </div>
    {/if}
  </div>

  {#if showBranchManager}
    <BranchManager
      repoId={$selectedRepo.id}
      repoName={$selectedRepo.name}
      cloneUrl={cloneUrl}
      on:close={() => showBranchManager = false}
      on:updated={loadRepoDetails}
    />
  {/if}
{/if}

<style>
  .repo-info-panel {
    background: var(--surface-color, #1e1e2e);
    border-radius: 8px;
    padding: 1rem;
    margin-top: 0.75rem;
  }

  .repo-info-panel.collapsed {
    padding-bottom: 0.5rem;
  }

  .info-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    width: 100%;
    background: none;
    border-top: none;
    border-left: none;
    border-right: none;
    cursor: pointer;
    text-align: left;
  }

  .repo-info-panel.collapsed .info-header {
    margin-bottom: 0;
    padding-bottom: 0;
    border-bottom: none;
  }

  .info-header:hover {
    opacity: 0.8;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .collapse-icon {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    width: 1rem;
  }

  .info-header h3 {
    margin: 0;
    font-size: 1rem;
    color: var(--text-color, #cdd6f4);
  }

  .status-badge {
    font-size: 0.7rem;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    background: var(--warning-color, #f9e2af);
    color: #1e1e2e;
    font-weight: 600;
    text-transform: uppercase;
  }

  .status-badge.ingested {
    background: var(--success-color, #a6e3a1);
  }

  .info-section {
    margin-bottom: 1rem;
  }

  .info-section:last-child {
    margin-bottom: 0;
  }

  .info-section label {
    display: block;
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 0.5rem;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .section-header label {
    margin-bottom: 0;
  }

  .btn-manage {
    font-size: 0.7rem;
    padding: 0.25rem 0.6rem;
    background: #cba6f7;
    color: #1e1e2e;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    font-weight: 500;
  }

  .btn-manage:hover {
    opacity: 0.9;
  }

  .info-section.warning {
    background: var(--warning-bg, rgba(249, 226, 175, 0.1));
    border: 1px solid var(--warning-color, #f9e2af);
    border-radius: 6px;
    padding: 0.75rem;
  }

  .info-section.warning p {
    margin: 0 0 0.5rem 0;
    color: var(--warning-color, #f9e2af);
    font-size: 0.9rem;
  }

  .url-box {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .url-box code {
    flex: 1;
    padding: 0.5rem;
    background: var(--surface-alt, #181825);
    border-radius: 4px;
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .btn-copy {
    padding: 0.5rem 0.75rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .btn-copy:hover {
    opacity: 0.9;
  }

  .instructions {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .instructions code {
    display: block;
    padding: 0.4rem 0.6rem;
    background: var(--surface-alt, #181825);
    border-radius: 4px;
    font-size: 0.72rem;
    color: var(--text-color, #cdd6f4);
    font-family: monospace;
    word-break: break-all;
  }

  .instructions p {
    margin: 0.5rem 0 0.25rem 0;
    font-size: 0.8rem;
  }

  .instruction-note {
    color: var(--text-muted, #6c7086);
    font-size: 0.75rem;
    margin: 0.5rem 0 0.25rem 0;
  }

  .instruction-note:first-child {
    margin-top: 0;
  }

  .cmd-group {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .cmd-group code {
    flex: 1;
    min-height: 1.8rem;
    display: flex;
    align-items: center;
  }

  .btn-copy-sm {
    padding: 0.2rem 0.4rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border: none;
    border-radius: 3px;
    cursor: pointer;
    font-size: 0.65rem;
    font-weight: 500;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .btn-copy-sm:hover {
    opacity: 0.9;
  }

  /*
    The branch list. These rules already existed, unused, from the list a
    <select> had replaced - the markup came back to them rather than growing a
    second parallel set (R3).
  */
  .branch-list {
    list-style: none;
    padding: 0;
    margin: 0;
    max-height: 180px;
    overflow-y: auto;
    background: var(--surface-alt, #181825);
    border-radius: 6px;
  }

  .branch-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    padding: 0.4rem 0.5rem;
    border: none;
    border-left: 2px solid transparent;
    border-radius: 4px;
    background: none;
    font-size: 0.8rem;
    font-family: inherit;
    text-align: left;
    cursor: pointer;
  }

  .branch-item:hover {
    background: var(--hover-color, #313244);
  }

  .branch-item.selected {
    background: var(--hover-color, #313244);
    border-left-color: var(--primary-color, #89b4fa);
  }

  .branch-item:focus-visible {
    outline: 2px solid var(--primary-color, #89b4fa);
    outline-offset: -2px;
  }

  .branch-name {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    min-width: 0;
    color: var(--text-color, #cdd6f4);
  }

  .branch-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .branch-empty p {
    margin: 0 0 0.35rem 0;
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
  }

  .branch-empty p:last-child {
    margin-bottom: 0;
  }

  .branch-failure {
    background: var(--warning-bg, rgba(249, 226, 175, 0.1));
    border: 1px solid var(--warning-color, #f9e2af);
    border-radius: 6px;
    padding: 0.6rem;
  }

  .failure {
    margin: 0 0 0.35rem 0;
    font-size: 0.8rem;
    color: var(--warning-color, #f9e2af);
  }

  .btn-retry {
    margin-top: 0.4rem;
    padding: 0.25rem 0.7rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: 500;
  }

  .btn-copy:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .commit {
    font-family: monospace;
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
  }

  .badge {
    font-size: 0.6rem;
    padding: 0.15rem 0.35rem;
    border-radius: 3px;
    text-transform: uppercase;
    font-weight: 600;
  }

  .badge.default {
    background: var(--primary-color, #89b4fa);
    color: #1e1e2e;
  }

  .badge.lazyaf {
    background: var(--accent-color, #cba6f7);
    color: #1e1e2e;
  }

  .remote-url {
    display: block;
    padding: 0.4rem 0.6rem;
    background: var(--surface-alt, #181825);
    border-radius: 4px;
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
    word-break: break-all;
  }

  .muted {
    color: var(--text-muted, #6c7086);
    font-size: 0.8rem;
    margin: 0;
  }

  /*
    `.branch-selector` went with the <select> it styled. Rules for markup that
    no longer exists are a lie the next reader has to disprove, and svelte
    warns on every one of them.
  */

  .git-graph {
    max-height: 300px;
    overflow-y: auto;
    background: var(--surface-alt, #181825);
    border-radius: 6px;
    padding: 0.5rem;
  }

  .commit-row {
    display: flex;
    gap: 0.75rem;
    min-height: 48px;
  }

  .graph-line {
    display: flex;
    flex-direction: column;
    align-items: center;
    width: 16px;
    flex-shrink: 0;
  }

  .node {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--text-muted, #6c7086);
    border: 2px solid var(--surface-alt, #181825);
    z-index: 1;
  }

  .node.head {
    background: var(--success-color, #a6e3a1);
    box-shadow: 0 0 6px var(--success-color, #a6e3a1);
  }

  .connector {
    width: 2px;
    flex: 1;
    background: var(--border-color, #45475a);
    margin-top: -2px;
  }

  .commit-info {
    flex: 1;
    min-width: 0;
    padding-bottom: 0.5rem;
  }

  .commit-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.25rem;
  }

  .commit-sha {
    font-family: monospace;
    font-size: 0.75rem;
    background: var(--badge-bg, #313244);
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    color: var(--primary-color, #89b4fa);
  }

  .branch-tag {
    font-size: 0.65rem;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    background: var(--border-color, #45475a);
    color: var(--text-color, #cdd6f4);
    font-weight: 600;
  }

  .branch-tag.default {
    background: var(--primary-color, #89b4fa);
    color: #1e1e2e;
  }

  .branch-tag.lazyaf {
    background: var(--accent-color, #cba6f7);
    color: #1e1e2e;
  }

  .commit-time {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    margin-left: auto;
  }

  .commit-message {
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 0.15rem;
  }

  .commit-author {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
  }
</style>
