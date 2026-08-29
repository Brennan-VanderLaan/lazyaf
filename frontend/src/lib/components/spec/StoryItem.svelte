<script lang="ts">
  import type { UserStory } from '../../api/types';
  import { specStore } from '../../stores/spec';
  import CriterionItem from './CriterionItem.svelte';
  import EditableRow from './EditableRow.svelte';

  export let story: UserStory;

  let editing = false;
  let editTitle = '';
  let editNarrative = '';
  let newCriterionText = '';
  let busy = false;

  $: expanded = $specStore.expandedStoryIds.includes(story.id);
  $: criteria = $specStore.criteriaByStory[story.id];

  function startEdit() {
    editTitle = story.title;
    editNarrative = story.narrative;
  }

  async function saveEdit() {
    await specStore.updateStory(story.id, {
      title: editTitle.trim(),
      narrative: editNarrative,
    });
  }

  async function addCriterion() {
    const text = newCriterionText.trim();
    if (!text || busy) return;
    busy = true;
    try {
      await specStore.createCriterion({ user_story_id: story.id, text, required: true });
      newCriterionText = '';
    } catch {
      // error surfaced via store
    } finally {
      busy = false;
    }
  }

  function handleNewCriterionKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') addCriterion();
  }
</script>

<div class="story" data-testid="story-item" data-story-id={story.id} data-expanded={expanded}>
  <div class="story-row">
    <button
      class="toggle"
      data-testid="story-toggle"
      aria-expanded={expanded}
      title={expanded ? 'Collapse story' : 'Expand story'}
      on:click={() => specStore.toggleStory(story.id)}
    >
      {expanded ? '▾' : '▸'}
    </button>

    <EditableRow
      entity="story"
      bind:editing
      bind:busy
      onStartEdit={startEdit}
      canSave={() => !!editTitle.trim()}
      onSave={saveEdit}
      confirmDelete={`Delete story "${story.title}" and its criteria?`}
      onDelete={() => specStore.deleteStory(story.id, story.feature_id)}
    >
      <svelte:fragment slot="edit" let:handleEscape>
        <input
          class="edit-input"
          data-testid="story-title-input"
          type="text"
          bind:value={editTitle}
          on:keydown={handleEscape}
        />
      </svelte:fragment>
      <svelte:fragment slot="display">
        <span class="story-title" data-testid="story-title">{story.title}</span>
        <span class="story-status" data-testid="story-status" data-status={story.status}>{story.status}</span>
        {#if story.priority !== null && story.priority !== undefined}
          <span class="story-priority" title="Priority">P{story.priority}</span>
        {/if}
      </svelte:fragment>
    </EditableRow>
  </div>

  {#if editing}
    <textarea
      class="narrative-input"
      data-testid="story-narrative-input"
      bind:value={editNarrative}
      rows="3"
      placeholder="Narrative (markdown OK): As a ... I want ... so that ..."
      on:keydown={(e) => { if (e.key === 'Escape') editing = false; }}
    ></textarea>
  {/if}

  {#if expanded}
    <div class="story-body">
      {#if !editing && story.narrative}
        <p class="narrative" data-testid="story-narrative">{story.narrative}</p>
      {/if}

      <div class="criteria">
        {#if criteria === undefined}
          <p class="hint">Loading criteria...</p>
        {:else}
          {#each criteria as criterion (criterion.id)}
            <CriterionItem {criterion} />
          {/each}
          {#if criteria.length === 0}
            <p class="hint">No acceptance criteria yet.</p>
          {/if}
        {/if}

        <div class="add-row">
          <input
            class="add-input"
            data-testid="new-criterion-text-input"
            type="text"
            placeholder="Add acceptance criterion..."
            bind:value={newCriterionText}
            on:keydown={handleNewCriterionKeydown}
          />
          <button
            class="btn-mini"
            data-testid="add-criterion-btn"
            disabled={busy || !newCriterionText.trim()}
            on:click={addCriterion}
          >
            + Criterion
          </button>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .story {
    border-left: 2px solid var(--border-color, #45475a);
    margin-left: 0.5rem;
    padding-left: 0.5rem;
  }

  .story-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 0.25rem;
    border-radius: 4px;
  }

  .story-row:hover {
    background: var(--hover-color, #313244);
  }

  .toggle {
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    cursor: pointer;
    font-size: 0.85rem;
    padding: 0 0.25rem;
    flex-shrink: 0;
  }

  .toggle:hover {
    color: var(--text-color, #cdd6f4);
  }

  .story-title {
    flex: 1;
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
  }

  .story-status {
    font-size: 0.7rem;
    padding: 0.1rem 0.45rem;
    border-radius: 4px;
    background: var(--badge-bg, #313244);
    color: var(--text-muted, #6c7086);
    text-transform: uppercase;
    flex-shrink: 0;
  }

  .story-status[data-status="active"] {
    background: rgba(137, 180, 250, 0.2);
    color: var(--primary-color, #89b4fa);
  }

  .story-status[data-status="done"] {
    background: rgba(166, 227, 161, 0.2);
    color: var(--success-color, #a6e3a1);
  }

  .story-priority {
    font-size: 0.7rem;
    color: var(--warning-color, #f9e2af);
    flex-shrink: 0;
  }

  .story-body {
    padding: 0.25rem 0 0.5rem 1.5rem;
  }

  .narrative {
    margin: 0.25rem 0 0.5rem;
    font-size: 0.82rem;
    color: var(--text-muted, #6c7086);
    white-space: pre-wrap;
  }

  .criteria {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
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
    padding: 0.35rem 0.5rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    font-family: inherit;
  }

  .add-input:focus,
  .edit-input:focus,
  .narrative-input:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  .narrative-input {
    width: 100%;
    margin: 0.35rem 0;
    padding: 0.5rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    font-family: inherit;
    resize: vertical;
  }

  .btn-mini {
    padding: 0.25rem 0.6rem;
    border-radius: 4px;
    border: none;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    flex-shrink: 0;
  }

  .btn-mini:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
