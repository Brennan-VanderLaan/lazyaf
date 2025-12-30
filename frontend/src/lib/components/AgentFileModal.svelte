<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { AgentFile } from '../api/types';
  import { agentFilesStore } from '../stores/agentFiles';

  export let agentFile: AgentFile | null = null;

  const dispatch = createEventDispatcher<{
    close: void;
    created: AgentFile;
    updated: AgentFile;
    deleted: void;
  }>();

  let name = agentFile?.name ?? '';
  let content = agentFile?.content ?? '';
  let description = agentFile?.description ?? '';
  let submitting = false;

  $: isEdit = agentFile !== null;

  async function handleSubmit() {
    if (!name.trim() || !content.trim()) return;
    submitting = true;

    try {
      if (isEdit && agentFile) {
        const updated = await agentFilesStore.update(agentFile.id, name, content, description || null);
        dispatch('updated', updated);
      } else {
        const created = await agentFilesStore.create(name, content, description || null);
        dispatch('created', created);
      }
    } finally {
      submitting = false;
    }
  }

  async function handleDelete() {
    if (!agentFile || !confirm('Are you sure you want to delete this agent file?')) return;
    submitting = true;
    try {
      await agentFilesStore.delete(agentFile.id);
      dispatch('deleted');
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
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="modal-backdrop" on:click={handleBackdropClick} role="dialog" aria-modal="true">
  <div class="modal">
    <div class="modal-header">
      <h2>{isEdit ? 'Edit Agent File' : 'New Agent File'}</h2>
      <button class="btn-close" on:click={() => dispatch('close')}>✕</button>
    </div>

    <form on:submit|preventDefault={handleSubmit}>
      <div class="form-group">
        <label for="name">Name</label>
        <input
          id="name"
          type="text"
          bind:value={name}
          placeholder="e.g., python-expert, documentation-writer"
          required
        />
      </div>

      <div class="form-group">
        <label for="description">Description</label>
        <input
          id="description"
          type="text"
          bind:value={description}
          placeholder="What does this agent do?"
        />
      </div>

      <div class="form-group">
        <label for="content">Content</label>
        <textarea
          id="content"
          bind:value={content}
          placeholder="Agent implementation, prompt, or script..."
          rows="12"
          required
        ></textarea>
      </div>

      {#if isEdit && agentFile}
        <div class="agent-meta">
          <div class="meta-item">
            <span class="meta-label">Created:</span>
            <span class="meta-value">{new Date(agentFile.created_at).toLocaleString()}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">Updated:</span>
            <span class="meta-value">{new Date(agentFile.updated_at).toLocaleString()}</span>
          </div>
        </div>
      {/if}

      <div class="modal-actions">
        {#if isEdit && agentFile}
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
              {submitting ? 'Creating...' : 'Create Agent File'}
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
    max-width: 800px;
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

  .form-group textarea {
    font-family: monospace;
    resize: vertical;
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

  .agent-meta {
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

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button:not(:disabled):hover {
    opacity: 0.9;
  }
</style>
