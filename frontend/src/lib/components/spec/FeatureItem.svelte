<script lang="ts">
  import type { Feature, FeatureStatus } from '../../api/types';
  import { specStore, featureStoryCounts } from '../../stores/spec';
  import StoryItem from './StoryItem.svelte';
  import EditableRow from './EditableRow.svelte';

  export let feature: Feature;

  const statusOptions: FeatureStatus[] = ['draft', 'active', 'done'];

  let editing = false;
  let editTitle = '';
  let editDescription = '';
  let editStatus: FeatureStatus = 'draft';
  let newStoryTitle = '';
  let busy = false;

  $: expanded = $specStore.expandedFeatureIds.includes(feature.id);
  $: stories = $specStore.storiesByFeature[feature.id] ?? [];
  $: storyCount = $featureStoryCounts[feature.id] ?? 0;

  function startEdit() {
    editTitle = feature.title;
    editDescription = feature.description;
    editStatus = feature.status;
  }

  async function saveEdit() {
    await specStore.updateFeature(feature.id, {
      title: editTitle.trim(),
      description: editDescription,
      status: editStatus,
    });
  }

  async function addStory() {
    const title = newStoryTitle.trim();
    if (!title || busy) return;
    busy = true;
    try {
      await specStore.createStory({ feature_id: feature.id, title });
      newStoryTitle = '';
    } catch {
      // error surfaced via store
    } finally {
      busy = false;
    }
  }

  function handleNewStoryKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') addStory();
  }
</script>

<div class="feature" data-testid="feature-item" data-feature-id={feature.id} data-expanded={expanded}>
  <div class="feature-row">
    <button
      class="toggle"
      data-testid="feature-toggle"
      aria-expanded={expanded}
      title={expanded ? 'Collapse feature' : 'Expand feature'}
      on:click={() => specStore.toggleFeature(feature.id)}
    >
      {expanded ? '▾' : '▸'}
    </button>

    <EditableRow
      entity="feature"
      bind:editing
      bind:busy
      onStartEdit={startEdit}
      canSave={() => !!editTitle.trim()}
      onSave={saveEdit}
      confirmDelete={`Delete feature "${feature.title}" and its stories?`}
      onDelete={() => specStore.deleteFeature(feature.id)}
    >
      <svelte:fragment slot="edit" let:handleEscape>
        <input
          class="edit-input"
          data-testid="feature-title-input"
          type="text"
          bind:value={editTitle}
          on:keydown={handleEscape}
        />
        <select class="status-select" data-testid="feature-status-select" bind:value={editStatus}>
          {#each statusOptions as status}
            <option value={status}>{status}</option>
          {/each}
        </select>
      </svelte:fragment>
      <svelte:fragment slot="display">
        <span class="feature-title" data-testid="feature-title">{feature.title}</span>
        <span class="feature-status" data-testid="feature-status" data-status={feature.status}>{feature.status}</span>
        <span class="story-count" data-testid="feature-story-count" title="{storyCount} user stories">
          {storyCount} {storyCount === 1 ? 'story' : 'stories'}
        </span>
      </svelte:fragment>
    </EditableRow>
  </div>

  {#if editing}
    <textarea
      class="description-input"
      data-testid="feature-description-input"
      bind:value={editDescription}
      rows="2"
      placeholder="What this feature is about..."
      on:keydown={(e) => { if (e.key === 'Escape') editing = false; }}
    ></textarea>
  {:else if expanded && feature.description}
    <p class="description" data-testid="feature-description">{feature.description}</p>
  {/if}

  {#if expanded}
    <div class="feature-body">
      {#each stories as story (story.id)}
        <StoryItem {story} />
      {/each}
      {#if stories.length === 0}
        <p class="hint">No user stories yet.</p>
      {/if}

      <div class="add-row">
        <input
          class="add-input"
          data-testid="new-story-title-input"
          type="text"
          placeholder="Add user story..."
          bind:value={newStoryTitle}
          on:keydown={handleNewStoryKeydown}
        />
        <button
          class="btn-mini"
          data-testid="add-story-btn"
          disabled={busy || !newStoryTitle.trim()}
          on:click={addStory}
        >
          + Story
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .feature {
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    padding: 0.5rem 0.75rem;
  }

  .feature-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .toggle {
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    cursor: pointer;
    font-size: 1rem;
    padding: 0 0.25rem;
    flex-shrink: 0;
  }

  .toggle:hover {
    color: var(--text-color, #cdd6f4);
  }

  .feature-title {
    flex: 1;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-color, #cdd6f4);
  }

  .feature-status {
    font-size: 0.7rem;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    background: var(--badge-bg, #313244);
    color: var(--text-muted, #6c7086);
    text-transform: uppercase;
    font-weight: 600;
    flex-shrink: 0;
  }

  .feature-status[data-status="active"] {
    background: rgba(137, 180, 250, 0.2);
    color: var(--primary-color, #89b4fa);
  }

  .feature-status[data-status="done"] {
    background: rgba(166, 227, 161, 0.2);
    color: var(--success-color, #a6e3a1);
  }

  .story-count {
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
    flex-shrink: 0;
  }

  .description {
    margin: 0.35rem 0 0 1.75rem;
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
    white-space: pre-wrap;
  }

  .feature-body {
    margin: 0.5rem 0 0.25rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .hint {
    margin: 0.25rem 0;
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .add-row {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.35rem;
  }

  .add-input,
  .edit-input {
    flex: 1;
    padding: 0.4rem 0.6rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.9rem;
    font-family: inherit;
  }

  .add-input:focus,
  .edit-input:focus,
  .description-input:focus,
  .status-select:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  .description-input {
    width: 100%;
    margin: 0.35rem 0 0;
    padding: 0.5rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    font-family: inherit;
    resize: vertical;
  }

  .status-select {
    padding: 0.35rem 0.5rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.8rem;
    cursor: pointer;
    flex-shrink: 0;
  }

  .btn-mini {
    padding: 0.3rem 0.65rem;
    border-radius: 4px;
    border: none;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    font-size: 0.78rem;
    font-weight: 500;
    cursor: pointer;
    flex-shrink: 0;
  }

  .btn-mini:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
