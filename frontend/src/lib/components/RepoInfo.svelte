<script lang="ts">
  import { onMount } from 'svelte';
  import { selectedRepo } from '../stores/repos';
  import { repos } from '../api/client';
  import type { BranchInfo, Commit } from '../api/types';

  let cloneUrl = '';
  let branches: BranchInfo[] = [];
  let commits: Commit[] = [];
  let loadingBranches = false;
  let loadingCommits = false;
  let copied = false;
  let selectedBranch: string | null = null;

  $: if ($selectedRepo) {
    loadRepoDetails();
  }

  $: if ($selectedRepo && selectedBranch) {
    loadCommits(selectedBranch);
  }

  async function loadRepoDetails() {
    if (!$selectedRepo) return;

    try {
      const urlResponse = await repos.cloneUrl($selectedRepo.id);
      cloneUrl = urlResponse.clone_url;
    } catch (e) {
      cloneUrl = '';
    }

    if ($selectedRepo.is_ingested) {
      loadingBranches = true;
      try {
        const branchResponse = await repos.branches($selectedRepo.id);
        branches = branchResponse.branches;
        // Auto-select default branch
        const defaultBranch = branches.find(b => b.is_default);
        if (defaultBranch && !selectedBranch) {
          selectedBranch = defaultBranch.name;
        }
      } catch (e) {
        branches = [];
      } finally {
        loadingBranches = false;
      }
    } else {
      branches = [];
      commits = [];
    }
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

  function formatTimestamp(timestamp: number): string {
    const date = new Date(timestamp * 1000);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  }

  function getBranchesAtCommit(sha: string): BranchInfo[] {
    return branches.filter(b => b.commit === sha);
  }
</script>

{#if $selectedRepo}
  <div class="repo-info-panel">
    <div class="info-header">
      <h3>{$selectedRepo.name}</h3>
      <span class="status-badge" class:ingested={$selectedRepo.is_ingested}>
        {$selectedRepo.is_ingested ? 'Ready' : 'Not Ingested'}
      </span>
    </div>

    {#if $selectedRepo.is_ingested}
      <div class="info-section">
        <label>Internal Git URL</label>
        <div class="url-box">
          <code>{cloneUrl || '...'}</code>
          <button
            class="btn-copy"
            on:click={() => copyToClipboard(cloneUrl)}
            title="Copy URL"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
      </div>

      <div class="info-section">
        <label>Push Updates</label>
        <div class="instructions">
          <code>git remote add lazyaf {cloneUrl}</code>
          <code>git push lazyaf {$selectedRepo.default_branch}</code>
        </div>
      </div>

      {#if branches.length > 0}
        <div class="info-section">
          <label>Branches ({branches.length})</label>
          <div class="branch-selector">
            <select bind:value={selectedBranch}>
              {#each branches as branch}
                <option value={branch.name}>
                  {branch.name}
                  {branch.is_default ? ' (default)' : ''}
                  {branch.is_lazyaf ? ' [agent]' : ''}
                </option>
              {/each}
            </select>
          </div>
        </div>

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
      {:else if loadingBranches}
        <div class="info-section">
          <label>Branches</label>
          <p class="muted">Loading...</p>
        </div>
      {:else}
        <div class="info-section">
          <label>Branches</label>
          <p class="muted">No branches yet. Push your repo to get started.</p>
        </div>
      {/if}
    {:else}
      <div class="info-section warning">
        <p>This repo needs to be ingested before agents can work on it.</p>
        <div class="instructions">
          <p><strong>From your repo directory, run:</strong></p>
          <code>lazyaf ingest</code>
          <p class="muted">Or use the API to ingest the repo first.</p>
        </div>
      </div>
    {/if}

    {#if $selectedRepo.remote_url}
      <div class="info-section">
        <label>Remote Origin</label>
        <code class="remote-url">{$selectedRepo.remote_url}</code>
      </div>
    {/if}
  </div>
{/if}

<style>
  .repo-info-panel {
    background: var(--surface-color, #1e1e2e);
    border-radius: 8px;
    padding: 1rem;
    margin-top: 1rem;
  }

  .info-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-color, #45475a);
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
    font-size: 0.75rem;
    color: var(--text-color, #cdd6f4);
    font-family: monospace;
  }

  .instructions p {
    margin: 0.5rem 0 0.25rem 0;
    font-size: 0.8rem;
  }

  .branch-list {
    list-style: none;
    padding: 0;
    margin: 0;
    max-height: 150px;
    overflow-y: auto;
  }

  .branch-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.4rem 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
  }

  .branch-item:hover {
    background: var(--hover-color, #313244);
  }

  .branch-item.lazyaf {
    background: var(--surface-alt, #181825);
  }

  .branch-name {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-color, #cdd6f4);
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

  .branch-selector {
    margin-bottom: 0.5rem;
  }

  .branch-selector select {
    width: 100%;
    padding: 0.5rem;
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .branch-selector select:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

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
