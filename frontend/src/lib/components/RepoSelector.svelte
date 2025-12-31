<script lang="ts">
  import { onMount } from 'svelte';
  import { reposStore, selectedRepoId, selectedRepo } from '../stores/repos';
  import type { RepoCreate } from '../api/types';
  import RepoInfo from './RepoInfo.svelte';

  let showAddForm = false;
  let newRepo: RepoCreate = { name: '', default_branch: 'main' };
  let submitting = false;

  onMount(() => {
    reposStore.load();
  });

  async function handleAdd() {
    if (!newRepo.name) return;
    submitting = true;
    try {
      const repo = await reposStore.ingest(newRepo);
      $selectedRepoId = repo.id;
      showAddForm = false;
      newRepo = { name: '', default_branch: 'main' };
    } finally {
      submitting = false;
    }
  }

  async function handleDelete(id: string) {
    if (!confirm('Are you sure you want to remove this repo?')) return;
    await reposStore.delete(id);
    if ($selectedRepoId === id) {
      $selectedRepoId = null;
    }
  }
</script>

<div class="repo-selector">
  <div class="repo-header">
    <h2>Repositories</h2>
    <button class="btn-icon" on:click={() => showAddForm = !showAddForm} title="Add repo">
      {showAddForm ? '✕' : '+'}
    </button>
  </div>

  {#if showAddForm}
    <form class="add-form" on:submit|preventDefault={handleAdd}>
      <input
        type="text"
        placeholder="Repository name"
        bind:value={newRepo.name}
        required
      />
      <input
        type="text"
        placeholder="Remote URL (optional, e.g., github.com/user/repo)"
        bind:value={newRepo.remote_url}
      />
      <input
        type="text"
        placeholder="Default branch"
        bind:value={newRepo.default_branch}
      />
      <button type="submit" class="btn-primary" disabled={submitting}>
        {submitting ? 'Creating...' : 'Create Repo'}
      </button>
      <p class="form-hint">After creating, push your local repo to the internal git URL.</p>
    </form>
  {/if}

  <ul class="repo-list">
    {#each $reposStore as repo}
      <li
        class="repo-item"
        class:selected={$selectedRepoId === repo.id}
        on:click={() => $selectedRepoId = repo.id}
        on:keydown={(e) => e.key === 'Enter' && ($selectedRepoId = repo.id)}
        role="button"
        tabindex="0"
      >
        <div class="repo-info">
          <div class="repo-name-row">
            <span class="repo-name">{repo.name}</span>
            <span class="repo-status" class:ready={repo.is_ingested} title={repo.is_ingested ? 'Ready' : 'Not ingested'}>
              {repo.is_ingested ? '●' : '○'}
            </span>
          </div>
          <span class="repo-branch">{repo.default_branch}</span>
        </div>
        <button
          class="btn-icon btn-delete"
          on:click|stopPropagation={() => handleDelete(repo.id)}
          title="Remove repo"
        >
          🗑
        </button>
      </li>
    {:else}
      <li class="repo-empty">No repositories added yet</li>
    {/each}
  </ul>

  <RepoInfo />
</div>

<style>
  .repo-selector {
    background: var(--surface-color, #1e1e2e);
    padding: 0 1rem 1rem;
    min-width: 280px;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .repo-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  .repo-header h2 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--text-color, #cdd6f4);
  }

  .btn-icon {
    background: none;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    cursor: pointer;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1rem;
  }

  .btn-icon:hover {
    background: var(--hover-color, #313244);
  }

  .add-form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-bottom: 1rem;
    padding: 0.75rem;
    background: var(--surface-alt, #181825);
    border-radius: 6px;
  }

  .add-form input {
    padding: 0.5rem;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    background: var(--input-bg, #1e1e2e);
    color: var(--text-color, #cdd6f4);
  }

  .add-form input::placeholder {
    color: var(--text-muted, #6c7086);
  }

  .btn-primary {
    padding: 0.5rem 1rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
  }

  .btn-primary:hover {
    opacity: 0.9;
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .form-hint {
    margin: 0.5rem 0 0 0;
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
  }

  .repo-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }

  .repo-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem;
    border-radius: 6px;
    cursor: pointer;
    margin-bottom: 0.25rem;
  }

  .repo-item:hover {
    background: var(--hover-color, #313244);
  }

  .repo-item.selected {
    background: var(--selected-color, #45475a);
  }

  .repo-info {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .repo-name-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .repo-name {
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
  }

  .repo-status {
    font-size: 0.6rem;
    color: var(--warning-color, #f9e2af);
  }

  .repo-status.ready {
    color: var(--success-color, #a6e3a1);
  }

  .repo-branch {
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
  }

  .btn-delete {
    opacity: 0;
    font-size: 0.8rem;
  }

  .repo-item:hover .btn-delete {
    opacity: 1;
  }

  .repo-empty {
    color: var(--text-muted, #6c7086);
    text-align: center;
    padding: 1rem;
  }
</style>
