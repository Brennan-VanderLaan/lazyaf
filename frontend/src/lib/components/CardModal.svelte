<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import type { Card, CardStatus, BranchInfo, MergeResult, RebaseResult } from '../api/types';
  import { cardsStore } from '../stores/cards';
  import { selectedRepo } from '../stores/repos';
  import { repos } from '../api/client';
  import JobStatus from './JobStatus.svelte';
  import DiffViewer from './DiffViewer.svelte';
  import ConflictResolver from './ConflictResolver.svelte';

  export let repoId: string;
  export let card: Card | null = null;

  // Branch state
  let branches: BranchInfo[] = [];
  let loadingBranches = false;
  let selectedTargetBranch: string = '';
  let mergeResult: MergeResult | null = null;
  let rebaseResult: RebaseResult | null = null;
  let diffRefreshKey: number = 0;  // Increment to force diff reload

  $: showDiff = card?.branch_name && (card?.status === 'in_review' || card?.status === 'done' || card?.status === 'failed');
  $: baseBranch = selectedTargetBranch || $selectedRepo?.default_branch || 'main';
  $: nonCardBranches = branches.filter(b => b.name !== card?.branch_name);

  const dispatch = createEventDispatcher<{
    close: void;
    created: Card;
    updated: Card;
    deleted: void;
  }>();

  let title = card?.title ?? '';
  let description = card?.description ?? '';
  let submitting = false;

  $: isEdit = card !== null;
  $: canStart = card?.status === 'todo';
  $: canApprove = card?.status === 'in_review';
  $: canReject = card?.status === 'in_review' || card?.status === 'in_progress';
  $: canRetry = card?.status === 'failed' || card?.status === 'in_review';
  $: canRebase = card?.branch_name && (card?.status === 'in_progress' || card?.status === 'in_review');

  onMount(async () => {
    if ($selectedRepo?.is_ingested) {
      loadBranches();
    }
  });

  async function loadBranches() {
    loadingBranches = true;
    try {
      const response = await repos.branches(repoId);
      branches = response.branches;
      // Set default target branch
      const defaultBranch = branches.find(b => b.is_default);
      if (defaultBranch) {
        selectedTargetBranch = defaultBranch.name;
      }
    } catch (e) {
      console.error('Failed to load branches:', e);
    } finally {
      loadingBranches = false;
    }
  }

  async function handleSubmit() {
    if (!title.trim()) return;
    submitting = true;

    try {
      if (isEdit && card) {
        const updated = await cardsStore.update(card.id, { title, description });
        dispatch('updated', updated);
      } else {
        const created = await cardsStore.create(repoId, { title, description });
        dispatch('created', created);
      }
    } finally {
      submitting = false;
    }
  }

  async function handleDelete() {
    if (!card || !confirm('Are you sure you want to delete this card?')) return;
    submitting = true;
    try {
      await cardsStore.delete(card.id);
      dispatch('deleted');
    } finally {
      submitting = false;
    }
  }

  async function handleStart() {
    if (!card) return;
    submitting = true;
    try {
      const updated = await cardsStore.start(card.id);
      dispatch('updated', updated);
    } finally {
      submitting = false;
    }
  }

  async function handleApprove() {
    if (!card) return;
    submitting = true;
    try {
      const response = await cardsStore.approve(card.id, selectedTargetBranch || undefined);
      mergeResult = response.merge_result;

      // Only close modal if merge succeeded
      // If there are conflicts or errors, keep modal open to show them
      if (response.merge_result?.success !== false) {
        dispatch('updated', response.card);
      }
    } catch (e) {
      // Show error but don't close modal
      alert(e instanceof Error ? e.message : 'Merge failed');
    } finally {
      submitting = false;
    }
  }

  async function handleReject() {
    if (!card) return;
    submitting = true;
    try {
      const updated = await cardsStore.reject(card.id);
      dispatch('updated', updated);
    } finally {
      submitting = false;
    }
  }

  async function handleRetry() {
    if (!card) return;
    submitting = true;
    try {
      const updated = await cardsStore.retry(card.id);
      dispatch('updated', updated);
    } finally {
      submitting = false;
    }
  }

  async function handleRebase() {
    if (!card) return;
    submitting = true;
    rebaseResult = null;
    try {
      const response = await cardsStore.rebase(card.id, selectedTargetBranch || undefined);
      rebaseResult = response.rebase_result;
      // Force diff to reload after rebase
      diffRefreshKey++;
      // Only close modal if rebase succeeded without conflicts
      if (rebaseResult?.success && !rebaseResult?.conflicts?.length) {
        dispatch('updated', response.card);
      }
    } catch (e) {
      // Show error but don't close modal
      alert(e instanceof Error ? e.message : 'Rebase failed');
    } finally {
      submitting = false;
    }
  }

  async function handleResolveConflicts(resolutions: Array<{ path: string; content: string }>) {
    if (!card) return;
    submitting = true;
    try {
      const response = await cardsStore.resolveConflicts(card.id, selectedTargetBranch || undefined, resolutions);
      mergeResult = response.merge_result;
      dispatch('updated', response.card);
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Conflict resolution failed');
    } finally {
      submitting = false;
    }
  }

  function handleCancelConflictResolution() {
    // Reset merge result to clear conflicts
    mergeResult = null;
  }

  async function handleResolveRebaseConflicts(resolutions: Array<{ path: string; content: string }>) {
    if (!card) return;
    submitting = true;
    try {
      const response = await cardsStore.resolveRebaseConflicts(card.id, selectedTargetBranch || undefined, resolutions);
      rebaseResult = response.rebase_result;
      // Force diff to reload after conflict resolution
      diffRefreshKey++;
      dispatch('updated', response.card);
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Conflict resolution failed');
    } finally {
      submitting = false;
    }
  }

  function handleCancelRebaseConflictResolution() {
    // Reset rebase result to clear conflicts
    rebaseResult = null;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      dispatch('close');
    }
  }

  function handleBackdropClick(e: MouseEvent) {
    if (e.target === e.currentTarget) {
      dispatch('close');
    }
  }

  const statusLabels: Record<CardStatus, string> = {
    todo: 'To Do',
    in_progress: 'In Progress',
    in_review: 'In Review',
    done: 'Done',
    failed: 'Failed',
  };
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="modal-backdrop" on:click={handleBackdropClick} role="dialog" aria-modal="true">
  <div class="modal">
    <div class="modal-header">
      <h2>{isEdit ? 'Edit Card' : 'New Card'}</h2>
      <button class="btn-close" on:click={() => dispatch('close')}>✕</button>
    </div>

    <form on:submit|preventDefault={handleSubmit}>
      <div class="form-group">
        <label for="title">Title</label>
        <input
          id="title"
          type="text"
          bind:value={title}
          placeholder="What needs to be done?"
          required
        />
      </div>

      <div class="form-group">
        <label for="description">Description</label>
        <textarea
          id="description"
          bind:value={description}
          placeholder="Describe the feature or task in detail..."
          rows="5"
        ></textarea>
      </div>

      {#if isEdit && card}
        <div class="card-meta">
          <div class="meta-item">
            <span class="meta-label">Status:</span>
            <span class="meta-value status-badge" data-status={card.status}>
              {statusLabels[card.status]}
            </span>
          </div>
          {#if card.branch_name}
            <div class="meta-item">
              <span class="meta-label">Branch:</span>
              <code class="meta-value">{card.branch_name}</code>
            </div>
          {/if}
          {#if card.pr_url}
            <div class="meta-item">
              <span class="meta-label">PR:</span>
              <a href={card.pr_url} target="_blank" rel="noopener" class="meta-value">
                View Pull Request
              </a>
            </div>
          {/if}
        </div>

        {#if card.job_id && (card.status === 'in_progress' || card.status === 'in_review' || card.status === 'failed')}
          <JobStatus cardId={card.id} jobId={card.job_id} />
        {/if}

        {#if showDiff && card.branch_name}
          <div class="diff-section">
            <div class="diff-header">
              <h3>Code Changes</h3>
              <div class="diff-header-actions">
                {#if nonCardBranches.length > 0 && (card.status === 'in_review' || card.status === 'in_progress')}
                  <div class="branch-selector">
                    <label for="target-branch">Target branch:</label>
                    <select id="target-branch" bind:value={selectedTargetBranch}>
                      {#each nonCardBranches as branch}
                        <option value={branch.name}>
                          {branch.name}
                          {branch.is_default ? ' (default)' : ''}
                        </option>
                      {/each}
                    </select>
                  </div>
                {/if}
                {#if canRebase}
                  <button
                    type="button"
                    class="btn-rebase"
                    on:click={handleRebase}
                    disabled={submitting}
                    title="Pull in latest changes from target branch"
                  >
                    Update Branch
                  </button>
                {/if}
              </div>
            </div>
            <DiffViewer {repoId} {baseBranch} headBranch={card.branch_name} refreshKey={diffRefreshKey} />
          </div>
        {/if}

        {#if rebaseResult}
          {#if rebaseResult.conflicts && rebaseResult.conflicts.length > 0}
            <ConflictResolver
              conflicts={rebaseResult.conflicts}
              onResolve={handleResolveRebaseConflicts}
              onCancel={handleCancelRebaseConflictResolution}
              operation="rebase"
              sourceBranch={card.branch_name || 'feature'}
              targetBranch={baseBranch}
            />
          {:else}
            <div class="rebase-result" class:success={rebaseResult.success}>
              <div class="result-icon">{rebaseResult.success ? '✓' : '✗'}</div>
              <div class="result-info">
                <div class="result-message">{rebaseResult.message}</div>
                {#if rebaseResult.new_sha}
                  <div class="result-sha">New commit: <code>{rebaseResult.new_sha.slice(0, 8)}</code></div>
                {/if}
              </div>
            </div>
          {/if}
        {/if}

        {#if mergeResult}
          {#if mergeResult.conflicts && mergeResult.conflicts.length > 0}
            <ConflictResolver
              conflicts={mergeResult.conflicts}
              onResolve={handleResolveConflicts}
              onCancel={handleCancelConflictResolution}
              operation="merge"
              sourceBranch={card?.branch_name || 'feature'}
              targetBranch={baseBranch}
            />
          {:else}
            <div class="merge-result" class:success={mergeResult.success}>
              <div class="merge-icon">{mergeResult.success ? '✓' : '✗'}</div>
              <div class="merge-info">
                <div class="merge-message">{mergeResult.message}</div>
                {#if mergeResult.merge_type}
                  <div class="merge-type">Type: {mergeResult.merge_type}</div>
                {/if}
                {#if mergeResult.new_sha}
                  <div class="merge-sha">New commit: <code>{mergeResult.new_sha.slice(0, 8)}</code></div>
                {/if}
              </div>
            </div>
          {/if}
        {/if}
      {/if}

      <div class="modal-actions">
        {#if isEdit && card}
          <div class="action-group-left">
            <button
              type="button"
              class="btn-delete"
              on:click={handleDelete}
              disabled={submitting}
            >
              Delete
            </button>
          </div>

          <div class="action-group-right">
            {#if canStart}
              <button
                type="button"
                class="btn-action btn-start"
                on:click={handleStart}
                disabled={submitting}
              >
                🚀 Start Work
              </button>
            {/if}
            {#if canApprove}
              <button
                type="button"
                class="btn-action btn-approve"
                on:click={handleApprove}
                disabled={submitting}
              >
                ✓ Approve
              </button>
            {/if}
            {#if canReject}
              <button
                type="button"
                class="btn-action btn-reject"
                on:click={handleReject}
                disabled={submitting}
              >
                ✗ Reject
              </button>
            {/if}
            {#if canRetry}
              <button
                type="button"
                class="btn-action btn-retry"
                on:click={handleRetry}
                disabled={submitting}
              >
                🔄 Retry
              </button>
            {/if}
            <button type="submit" class="btn-primary" disabled={submitting}>
              {submitting ? 'Saving...' : 'Save'}
            </button>
          </div>
        {:else}
          <div class="action-group-right">
            <button type="button" class="btn-secondary" on:click={() => dispatch('close')}>
              Cancel
            </button>
            <button type="submit" class="btn-primary" disabled={submitting}>
              {submitting ? 'Creating...' : 'Create Card'}
            </button>
          </div>
        {/if}
      </div>
    </form>
  </div>
</div>

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }

  .modal {
    background: var(--surface-color, #1e1e2e);
    border-radius: 12px;
    width: calc(100% - 2rem);
    max-width: min(1400px, 95vw);
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  }

  @media (max-width: 900px) {
    .modal {
      max-width: 100%;
      border-radius: 8px;
    }
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.25rem 1.5rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    color: var(--text-color, #cdd6f4);
  }

  .btn-close {
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    font-size: 1.25rem;
    cursor: pointer;
    padding: 0.25rem;
  }

  .btn-close:hover {
    color: var(--text-color, #cdd6f4);
  }

  form {
    padding: 1.5rem;
  }

  .form-group {
    margin-bottom: 1.25rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
  }

  .form-group input,
  .form-group textarea {
    width: 100%;
    padding: 0.75rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.95rem;
    font-family: inherit;
  }

  .form-group input::placeholder,
  .form-group textarea::placeholder {
    color: var(--text-muted, #6c7086);
  }

  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  .card-meta {
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1.25rem;
  }

  .meta-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }

  .meta-item:last-child {
    margin-bottom: 0;
  }

  .meta-label {
    color: var(--text-muted, #6c7086);
    font-size: 0.85rem;
  }

  .meta-value {
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
  }

  .meta-value code {
    font-family: monospace;
    background: var(--badge-bg, #313244);
    padding: 0.15rem 0.4rem;
    border-radius: 4px;
  }

  .status-badge {
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-weight: 500;
  }

  .status-badge[data-status="todo"] { background: #89b4fa33; color: #89b4fa; }
  .status-badge[data-status="in_progress"] { background: #f9e2af33; color: #f9e2af; }
  .status-badge[data-status="in_review"] { background: #cba6f733; color: #cba6f7; }
  .status-badge[data-status="done"] { background: #a6e3a133; color: #a6e3a1; }
  .status-badge[data-status="failed"] { background: #f38ba833; color: #f38ba8; }

  .diff-section {
    margin-top: 1.25rem;
    padding-top: 1.25rem;
    border-top: 1px solid var(--border-color, #45475a);
  }

  .diff-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .diff-section h3 {
    margin: 0;
    font-size: 1rem;
    color: var(--text-color, #cdd6f4);
  }

  .diff-header-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .branch-selector {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .branch-selector label {
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
  }

  .branch-selector select {
    padding: 0.4rem 0.75rem;
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

  .btn-rebase {
    padding: 0.4rem 0.75rem;
    background: #89b4fa;
    color: #1e1e2e;
    border: none;
    border-radius: 4px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
  }

  .btn-rebase:hover:not(:disabled) {
    opacity: 0.9;
  }

  .btn-rebase:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .rebase-result {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 1rem;
    margin-top: 1rem;
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    border: 1px solid var(--border-color, #45475a);
  }

  .rebase-result.success {
    border-color: var(--success-color, #a6e3a1);
    background: rgba(166, 227, 161, 0.1);
  }

  .result-icon {
    font-size: 1.25rem;
    line-height: 1;
  }

  .rebase-result.success .result-icon {
    color: var(--success-color, #a6e3a1);
  }

  .result-info, .notice-info {
    flex: 1;
  }

  .result-message, .notice-message {
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
    margin-bottom: 0.25rem;
  }

  .result-sha, .notice-detail {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .result-sha code, .notice-detail {
    font-family: monospace;
    background: var(--badge-bg, #313244);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    color: var(--primary-color, #89b4fa);
  }

  .merge-result {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 1rem;
    margin-top: 1rem;
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    border: 1px solid var(--border-color, #45475a);
  }

  .merge-result.success {
    border-color: var(--success-color, #a6e3a1);
    background: rgba(166, 227, 161, 0.1);
  }

  .merge-icon {
    font-size: 1.25rem;
    line-height: 1;
  }

  .merge-result.success .merge-icon {
    color: var(--success-color, #a6e3a1);
  }

  .merge-info {
    flex: 1;
  }

  .merge-message {
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
    margin-bottom: 0.25rem;
  }

  .merge-type, .merge-sha {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .merge-sha code {
    font-family: monospace;
    background: var(--badge-bg, #313244);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    color: var(--primary-color, #89b4fa);
  }

  .modal-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color, #45475a);
  }

  .action-group-left {
    display: flex;
    gap: 0.5rem;
  }

  .action-group-right {
    display: flex;
    gap: 0.5rem;
    margin-left: auto;
  }

  .btn-primary,
  .btn-secondary,
  .btn-action,
  .btn-delete {
    padding: 0.6rem 1rem;
    border-radius: 6px;
    font-weight: 500;
    cursor: pointer;
    border: none;
    font-size: 0.9rem;
  }

  .btn-primary {
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
  }

  .btn-secondary {
    background: var(--surface-alt, #313244);
    color: var(--text-color, #cdd6f4);
  }

  .btn-delete {
    background: transparent;
    color: var(--error-color, #f38ba8);
    border: 1px solid var(--error-color, #f38ba8);
  }

  .btn-start {
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
  }

  .btn-approve {
    background: #a6e3a1;
    color: #1e1e2e;
  }

  .btn-reject {
    background: transparent;
    color: var(--error-color, #f38ba8);
    border: 1px solid var(--error-color, #f38ba8);
  }

  .btn-retry {
    background: #f9e2af;
    color: #1e1e2e;
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button:not(:disabled):hover {
    opacity: 0.9;
  }
</style>
