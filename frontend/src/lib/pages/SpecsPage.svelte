<script lang="ts">
  import { onMount } from 'svelte';
  import { specStore } from '../stores/spec';
  import FeatureItem from '../components/spec/FeatureItem.svelte';

  let showCreate = false;
  let newTitle = '';
  let newDescription = '';
  let creating = false;

  onMount(() => {
    specStore.loadAll();
  });

  async function handleCreateFeature() {
    const title = newTitle.trim();
    if (!title || creating) return;
    creating = true;
    try {
      const feature = await specStore.createFeature({
        title,
        description: newDescription.trim(),
      });
      newTitle = '';
      newDescription = '';
      showCreate = false;
      // Expand the new feature so a story can be added immediately
      specStore.toggleFeature(feature.id);
    } catch {
      // error surfaced via store banner
    } finally {
      creating = false;
    }
  }

  async function handleSeed() {
    try {
      await specStore.seedMilestone12();
    } catch {
      // error surfaced via store banner
    }
  }

  function handleNewFeatureKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') handleCreateFeature();
    if (e.key === 'Escape') showCreate = false;
  }
</script>

<div class="specs-page" data-testid="specs-page">
  <header class="page-header">
    <div class="header-left">
      <h1>Specs</h1>
      <span class="subtitle">Features → user stories → acceptance criteria</span>
    </div>
    <button
      class="btn-primary"
      data-testid="add-feature-btn"
      on:click={() => showCreate = !showCreate}
    >
      + New Feature
    </button>
  </header>

  {#if $specStore.error}
    <div class="error-banner" data-testid="spec-error">{$specStore.error}</div>
  {/if}

  {#if showCreate}
    <div class="create-form" data-testid="create-feature-form">
      <input
        data-testid="new-feature-title-input"
        type="text"
        placeholder="Feature title..."
        bind:value={newTitle}
        on:keydown={handleNewFeatureKeydown}
      />
      <input
        data-testid="new-feature-description-input"
        type="text"
        placeholder="Short description (optional)..."
        bind:value={newDescription}
        on:keydown={handleNewFeatureKeydown}
      />
      <div class="create-actions">
        <button
          class="btn-primary"
          data-testid="create-feature-btn"
          disabled={creating || !newTitle.trim()}
          on:click={handleCreateFeature}
        >
          {creating ? 'Creating...' : 'Create'}
        </button>
        <button
          class="btn-secondary"
          data-testid="cancel-create-feature-btn"
          on:click={() => showCreate = false}
        >
          Cancel
        </button>
      </div>
    </div>
  {/if}

  <div class="content">
    {#if $specStore.loading && $specStore.features.length === 0}
      <p class="loading-hint">Loading specs...</p>
    {:else if $specStore.features.length === 0}
      <div class="empty-state">
        <span class="empty-icon">📐</span>
        <p>No features yet. Create one inline, or seed the Milestone 12 north-star stories.</p>
        <button
          class="btn-primary"
          data-testid="seed-milestone12-btn"
          disabled={$specStore.seeding}
          on:click={handleSeed}
        >
          {$specStore.seeding ? 'Seeding...' : 'Seed Milestone 12'}
        </button>
      </div>
    {:else}
      <div class="feature-list" data-testid="feature-list">
        {#each $specStore.features as feature (feature.id)}
          <FeatureItem {feature} />
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  /* See the note in PipelinesPage: `overflow: hidden` made content that did
     not fit permanently unreachable at narrow widths instead of merely
     off-screen. No scrollbar appears at any width where the content fits. */
  .specs-page {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 1.5rem 2rem;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    flex-wrap: wrap;
    gap: 0.75rem;
  }

  .header-left {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
  }

  .page-header h1 {
    margin: 0;
    font-size: 1.5rem;
    color: var(--text-color, #cdd6f4);
  }

  .subtitle {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .error-banner {
    background: rgba(243, 139, 168, 0.15);
    border: 1px solid var(--error-color, #f38ba8);
    color: var(--error-color, #f38ba8);
    border-radius: 6px;
    padding: 0.6rem 0.9rem;
    margin-bottom: 0.75rem;
    font-size: 0.85rem;
  }

  .create-form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--primary-color, #89b4fa);
    border-radius: 8px;
    padding: 0.75rem;
    margin-bottom: 1rem;
  }

  .create-form input {
    padding: 0.5rem 0.7rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.9rem;
    font-family: inherit;
  }

  .create-form input:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  .create-actions {
    display: flex;
    gap: 0.5rem;
  }

  .content {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  }

  .loading-hint {
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 3rem 1rem;
    color: var(--text-muted, #6c7086);
    text-align: center;
  }

  .empty-icon {
    font-size: 2.5rem;
  }

  .feature-list {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding-bottom: 1.5rem;
  }

  .btn-primary {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    border: none;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
  }

  .btn-secondary {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    border: none;
    background: var(--surface-alt, #313244);
    color: var(--text-color, #cdd6f4);
    font-size: 0.9rem;
    cursor: pointer;
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button:not(:disabled):hover {
    opacity: 0.9;
  }
</style>
