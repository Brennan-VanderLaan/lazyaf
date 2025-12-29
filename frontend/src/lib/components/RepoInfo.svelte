<script lang="ts">
  import { onMount } from 'svelte';
  import { selectedRepo } from '../stores/repos';
  import { repos } from '../api/client';
  import type { BranchInfo } from '../api/types';

  let cloneUrl = '';
  let branches: BranchInfo[] = [];
  let loadingBranches = false;
  let copied = false;

  $: if ($selectedRepo) {
    loadRepoDetails();
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
      } catch (e) {
        branches = [];
      } finally {
        loadingBranches = false;
      }
    } else {
      branches = [];
    }
  }

  async function copyToClipboard(text: string) {
    await navigator.clipboard.writeText(text);
    copied = true;
    setTimeout(() => copied = false, 2000);
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
          <ul class="branch-list">
            {#each branches as branch}
              <li class="branch-item" class:lazyaf={branch.is_lazyaf}>
                <span class="branch-name">
                  {branch.name}
                  {#if branch.is_default}
                    <span class="badge default">default</span>
                  {/if}
                  {#if branch.is_lazyaf}
                    <span class="badge lazyaf">agent</span>
                  {/if}
                </span>
                <span class="commit">{branch.commit?.slice(0, 7) || ''}</span>
              </li>
            {/each}
          </ul>
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
</style>
