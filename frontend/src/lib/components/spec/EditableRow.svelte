<script lang="ts">
  /**
   * Shared inline-edit state machine for the spec tree rows (Feature / Story /
   * Criterion). Owns the editing/busy flags and the Save / Cancel / Edit /
   * Delete buttons (uniform data-testids: `save-{entity}-btn`,
   * `cancel-{entity}-edit-btn`, `edit-{entity}-btn`, `delete-{entity}-btn`);
   * the entity-specific display and edit fields come in through slots.
   *
   * Renders no wrapper element so the slots and buttons sit directly inside
   * the parent's flex row. `editing`/`busy` are bindable: parents use them to
   * show edit-mode blocks outside this row (description/narrative textareas)
   * and to share the busy flag with sibling actions (e.g. "add story").
   *
   * Escape cancels an in-progress edit everywhere: the slot props
   * `handleKeydown` (Enter saves + Escape cancels) and `handleEscape`
   * (Escape only) are for the parent to attach to its edit inputs.
   */
  export let entity: string; // 'feature' | 'story' | 'criterion' - testid/title suffix
  export let editing = false;
  export let busy = false;
  /** Populate the parent's edit fields from current values before entering edit mode. */
  export let onStartEdit: () => void = () => {};
  /** Persist the edit; throw to stay in edit mode (error surfaced via store). */
  export let onSave: () => Promise<void>;
  /** Delete the row; errors are surfaced via the store. */
  export let onDelete: () => Promise<void>;
  /** Optional confirm() prompt shown before onDelete runs. */
  export let confirmDelete: string | null = null;
  /** Validation gate: saving is a no-op while this returns false. */
  export let canSave: () => boolean = () => true;

  function startEdit() {
    onStartEdit();
    editing = true;
  }

  function cancelEdit() {
    editing = false;
  }

  async function save() {
    if (!canSave() || busy) return;
    busy = true;
    try {
      await onSave();
      editing = false;
    } catch {
      // error surfaced via store
    } finally {
      busy = false;
    }
  }

  async function remove() {
    if (busy) return;
    if (confirmDelete && !confirm(confirmDelete)) return;
    busy = true;
    try {
      await onDelete();
    } catch {
      // error surfaced via store
    } finally {
      busy = false;
    }
  }

  function handleEscape(e: KeyboardEvent) {
    if (e.key === 'Escape') cancelEdit();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') save();
    if (e.key === 'Escape') cancelEdit();
  }
</script>

{#if editing}
  <slot name="edit" {busy} {handleKeydown} {handleEscape} />
  <button class="btn-mini" data-testid="save-{entity}-btn" disabled={busy} on:click={save}>Save</button>
  <button class="btn-mini ghost" data-testid="cancel-{entity}-edit-btn" on:click={cancelEdit}>Cancel</button>
{:else}
  <slot name="display" {busy} />
  <button class="btn-mini ghost" data-testid="edit-{entity}-btn" title="Edit {entity}" on:click={startEdit}>✎</button>
  <button class="btn-mini ghost danger" data-testid="delete-{entity}-btn" title="Delete {entity}" disabled={busy} on:click={remove}>✕</button>
{/if}

<style>
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

  .btn-mini.ghost {
    background: transparent;
    color: var(--text-muted, #6c7086);
  }

  .btn-mini.ghost:hover {
    color: var(--text-color, #cdd6f4);
  }

  .btn-mini.danger:hover {
    color: var(--error-color, #f38ba8);
  }

  .btn-mini:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
