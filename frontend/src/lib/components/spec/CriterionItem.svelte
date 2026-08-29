<script lang="ts">
  import type { AcceptanceCriterion } from '../../api/types';
  import { specStore } from '../../stores/spec';
  import EditableRow from './EditableRow.svelte';

  export let criterion: AcceptanceCriterion;

  let editing = false;
  let editText = '';
  let busy = false;

  function startEdit() {
    editText = criterion.text;
  }

  async function saveEdit() {
    await specStore.updateCriterion(criterion.id, { text: editText.trim() });
  }

  async function toggleRequired() {
    if (busy) return;
    busy = true;
    try {
      await specStore.updateCriterion(criterion.id, { required: !criterion.required });
    } catch {
      // error surfaced via store
    } finally {
      busy = false;
    }
  }
</script>

<div class="criterion" data-testid="criterion-item" data-criterion-id={criterion.id}>
  <label class="required-toggle" title={criterion.required ? 'Required' : 'Optional'}>
    <input
      type="checkbox"
      data-testid="criterion-required-checkbox"
      checked={criterion.required}
      disabled={busy}
      on:change={toggleRequired}
    />
    <span class="required-label">req</span>
  </label>

  <EditableRow
    entity="criterion"
    bind:editing
    bind:busy
    onStartEdit={startEdit}
    canSave={() => !!editText.trim()}
    onSave={saveEdit}
    onDelete={() => specStore.deleteCriterion(criterion.id, criterion.user_story_id)}
  >
    <svelte:fragment slot="edit" let:handleKeydown>
      <input
        class="edit-input"
        data-testid="criterion-text-input"
        type="text"
        bind:value={editText}
        on:keydown={handleKeydown}
      />
    </svelte:fragment>
    <svelte:fragment slot="display">
      <span class="criterion-text" data-testid="criterion-text">{criterion.text}</span>
      {#if criterion.notes}
        <span class="criterion-notes" title={criterion.notes}>📝</span>
      {/if}
    </svelte:fragment>
  </EditableRow>
</div>

<style>
  .criterion {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 0.5rem;
    border-radius: 4px;
  }

  .criterion:hover {
    background: var(--hover-color, #313244);
  }

  .required-toggle {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    cursor: pointer;
    flex-shrink: 0;
  }

  .required-toggle input {
    margin: 0;
    cursor: pointer;
  }

  .required-label {
    font-size: 0.65rem;
    text-transform: uppercase;
    color: var(--text-muted, #6c7086);
  }

  .criterion-text {
    flex: 1;
    font-size: 0.85rem;
    color: var(--text-color, #cdd6f4);
  }

  .criterion-notes {
    font-size: 0.75rem;
    cursor: help;
  }

  .edit-input {
    flex: 1;
    padding: 0.3rem 0.5rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    font-family: inherit;
  }

  .edit-input:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }
</style>
