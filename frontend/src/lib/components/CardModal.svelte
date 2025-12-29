<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { Card, CardStatus } from '../api/types';
  import { cardsStore } from '../stores/cards';

  export let repoId: string;
  export let card: Card | null = null;

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
      const updated = await cardsStore.approve(card.id);
      dispatch('updated', updated);
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
    width: 100%;
    max-width: 560px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
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

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button:not(:disabled):hover {
    opacity: 0.9;
  }
</style>
