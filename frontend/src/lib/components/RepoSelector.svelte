<script context="module" lang="ts">
  import { writable } from 'svelte/store';

  /**
   * A request from elsewhere in the app to open this form.
   *
   * The board's first-run panel (pages/BoardPage.svelte) is the biggest thing
   * on a cold-open screen, and the newcomer probe found it offered no control
   * at all: the only way into the app was an unlabelled 20px `+` in the
   * sidebar chrome. Rather than clone the form onto the board - two forms
   * POSTing the same contract - that panel sets this flag and the sidebar
   * opens and focuses the REAL one. Set-and-clear, so it fires once per ask.
   */
  export const addRepoFormRequested = writable(false);

  /**
   * True once a repo list has actually come back from the backend.
   *
   * "The list is empty" and "the list has not arrived" are the same value,
   * and surfaces that teach a newcomer (the board's first-run panel) must not
   * confuse them. This lives here because this component is the ONE caller of
   * `reposStore.load()`, and it lives at module scope because pages unmount:
   * an instance-scoped flag would reset every time the user navigated back to
   * the board, and the load never happens a second time.
   */
  export const reposEverLoaded = writable(false);

  /**
   * Why the repo LIST failed to load, or null when it loaded fine.
   *
   * Deliberately not `reposStore.error`: that field is the last error from ANY
   * repo operation, so a refused CREATE lands in it too. Reading it from the
   * board made a rejected 200-character name paint "Could not load your
   * repositories" across the whole page while the form underneath was already
   * explaining the real problem. This is a snapshot of one operation's
   * outcome, taken the moment that operation finishes.
   */
  export const reposLoadError = writable<string | null>(null);
</script>

<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { reposStore, selectedRepoId, selectedRepo } from '../stores/repos';
  import type { RepoCreate } from '../api/types';
  import RepoInfo from './RepoInfo.svelte';
  // QA triage T7: the connection indicator has to live inside an
  // always-mounted component. This is the topmost one under src/lib/;
  // its natural home is App.svelte's sidebar, above <RepoSelector />.
  import ConnectionStatus from './ConnectionStatus.svelte';

  /** Backend's own words for why the last create was refused. See handleAdd. */
  let createError: string | null = null;

  let showAddForm = false;
  let newRepo: RepoCreate = { name: '', default_branch: 'main' };
  let submitting = false;
  let nameInput: HTMLInputElement | undefined;

  onMount(async () => {
    // `load()` swallows its own failure into reposStore.error and always
    // resolves, so this marks "we looked", not "we succeeded" - which is
    // exactly the distinction the board's empty states need. Snapshot the
    // outcome BEFORE announcing the load, and before any later create/delete
    // can overwrite that shared field.
    await reposStore.load();
    reposLoadError.set(get(reposStore.error));
    reposEverLoaded.set(true);
  });

  $: if ($addRepoFormRequested) {
    addRepoFormRequested.set(false);
    openAddForm();
  }

  async function openAddForm() {
    createError = null;
    showAddForm = true;
    // The caller is somewhere else on the page; land the cursor in the field
    // so a newcomer sent here from the board can just start typing.
    await tick();
    nameInput?.focus();
  }

  function toggleAddForm() {
    if (showAddForm) {
      showAddForm = false;
      createError = null;
    } else {
      openAddForm();
    }
  }

  async function handleAdd() {
    if (!newRepo.name.trim()) return;
    submitting = true;
    createError = null;
    try {
      const repo = await reposStore.ingest(newRepo);
      $selectedRepoId = repo.id;
      showAddForm = false;
      newRepo = { name: '', default_branch: 'main' };
    } catch (e) {
      // R1: the store re-throws precisely so this cannot be swallowed. This
      // used to be a bare `finally`, so a 422 (e.g. a pasted path over the
      // 200-char limit) flipped the button back to "Create Repo" and said
      // NOTHING - the first click a stranger makes, failing in the dark.
      // api/client.ts has already turned FastAPI's detail array into a
      // sentence; show that sentence.
      createError = e instanceof Error ? e.message : 'Could not create the repository.';
    } finally {
      submitting = false;
    }
  }

  async function handleDelete(id: string) {
    // Name the target. The rows are live-updated, so "this repo" is not
    // enough to catch a row that moved under the cursor.
    const repo = $reposStore.find(r => r.id === id);
    if (!confirm(`Remove repository "${repo?.name ?? id}" from LazyAF?`)) return;
    await reposStore.delete(id);
    if ($selectedRepoId === id) {
      $selectedRepoId = null;
    }
  }
</script>

<ConnectionStatus />

<div class="repo-selector" data-testid="repo-selector">
  <div class="repo-header">
    <h2>Repositories</h2>
    <button
      class="btn-icon"
      data-testid="add-repo-btn"
      on:click={toggleAddForm}
      title={showAddForm ? 'Close' : 'Add repository'}
      aria-label={showAddForm ? 'Close add repository form' : 'Add repository'}
      aria-expanded={showAddForm}
    >
      {showAddForm ? '✕' : '+'}
    </button>
  </div>

  {#if showAddForm}
    <form class="add-form" on:submit|preventDefault={handleAdd}>
      <label class="field-label" for="repo-name">Repository name</label>
      <input
        id="repo-name"
        type="text"
        name="repo-name"
        data-testid="repo-name-input"
        placeholder="my-project"
        maxlength="200"
        bind:value={newRepo.name}
        bind:this={nameInput}
        required
      />
      <label class="field-label" for="repo-remote-url">Remote URL <span class="field-optional">(optional)</span></label>
      <input
        id="repo-remote-url"
        type="text"
        placeholder="github.com/user/repo"
        bind:value={newRepo.remote_url}
      />
      <label class="field-label" for="repo-default-branch">Default branch</label>
      <input
        id="repo-default-branch"
        type="text"
        placeholder="Default branch"
        bind:value={newRepo.default_branch}
      />
      {#if createError}
        <p class="form-error" data-testid="repo-create-error" role="alert">{createError}</p>
      {/if}
      <button type="submit" class="btn-primary" disabled={submitting}>
        {submitting ? 'Creating...' : 'Create Repo'}
      </button>
      <p class="form-hint">
        LazyAF hosts its own copy of your code. After creating, push to the
        internal git URL — the exact commands appear under
        <strong>Repository Details</strong>, below this list.
      </p>
    </form>
  {/if}

  <ul class="repo-list">
    <!--
      Keyed by id. Unkeyed, Svelte reuses these <li> elements by POSITION, so a
      repo_created/repo_deleted frame rewrites the row under the cursor - the
      QA probe measured the identical trash-can node moving from one repo to
      another - and the click lands on a repository the user never aimed at.
    -->
    {#each $reposStore as repo (repo.id)}
      <li
        class="repo-item"
        data-testid="repo-item"
        data-repo-id={repo.id}
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

  .field-label {
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--text-muted, #6c7086);
    margin-bottom: -0.25rem;
  }

  .field-optional {
    text-transform: none;
    font-weight: 400;
    letter-spacing: 0;
  }

  .form-error {
    margin: 0;
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--error-color, #f38ba8);
    border-radius: 4px;
    background: rgba(243, 139, 168, 0.12);
    color: var(--error-color, #f38ba8);
    font-size: 0.8rem;
    line-height: 1.4;
    word-break: break-word;
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
