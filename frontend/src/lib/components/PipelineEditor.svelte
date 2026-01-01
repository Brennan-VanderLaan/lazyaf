<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte';
  import type { Pipeline, PipelineStepConfig, StepType, RunnerType } from '../api/types';
  import { pipelinesStore } from '../stores/pipelines';

  export let repoId: string;
  export let pipeline: Pipeline | null = null;

  const dispatch = createEventDispatcher<{
    close: void;
    created: Pipeline;
    updated: Pipeline;
    deleted: void;
  }>();

  // Normalize step to ensure all fields have defaults
  function normalizeStep(step: Partial<PipelineStepConfig>): PipelineStepConfig {
    return {
      name: step.name || 'Unnamed Step',
      type: step.type || 'script',
      config: step.config || {},
      on_success: step.on_success || 'next',
      on_failure: step.on_failure || 'stop',
      timeout: step.timeout || 300,
      continue_in_context: step.continue_in_context ?? false,
    };
  }

  // Track original values to detect changes
  const originalName = pipeline?.name || '';
  const originalDescription = pipeline?.description || '';
  const originalSteps = JSON.stringify(pipeline?.steps || []);

  let name = pipeline?.name || '';
  let description = pipeline?.description || '';
  let steps: PipelineStepConfig[] = (pipeline?.steps || []).map(normalizeStep);
  let submitting = false;

  $: isEdit = pipeline !== null;

  // Check if there are unsaved changes
  $: hasChanges = name !== originalName ||
                  description !== originalDescription ||
                  JSON.stringify(steps) !== originalSteps;

  function createDefaultStep(): PipelineStepConfig {
    return {
      name: `Step ${steps.length + 1}`,
      type: 'script',
      config: { command: '' },
      on_success: 'next',
      on_failure: 'stop',
      timeout: 300,
      continue_in_context: false,
    };
  }

  function addStep() {
    steps = [...steps, createDefaultStep()];
  }

  function removeStep(index: number) {
    steps = steps.filter((_, i) => i !== index);
  }

  function moveStep(index: number, direction: 'up' | 'down') {
    const newIndex = direction === 'up' ? index - 1 : index + 1;
    if (newIndex < 0 || newIndex >= steps.length) return;

    const newSteps = [...steps];
    [newSteps[index], newSteps[newIndex]] = [newSteps[newIndex], newSteps[index]];
    steps = newSteps;
  }

  function updateStep(index: number, step: PipelineStepConfig) {
    steps = steps.map((s, i) => i === index ? step : s);
  }

  function updateStepType(index: number, type: StepType) {
    const step = { ...steps[index], type };
    // Reset config based on type
    if (type === 'script') {
      step.config = { command: '' };
    } else if (type === 'docker') {
      step.config = { image: '', command: '' };
    } else if (type === 'agent') {
      step.config = { runner_type: 'any', title: '', description: '' };
    }
    updateStep(index, step);
  }

  async function handleSubmit() {
    if (!name.trim() || steps.length === 0) return;
    submitting = true;

    try {
      const data = {
        name: name.trim(),
        description: description.trim() || undefined,
        steps,
      };

      if (isEdit && pipeline) {
        const updated = await pipelinesStore.update(pipeline.id, data);
        dispatch('updated', updated);
      } else {
        const created = await pipelinesStore.create(repoId, data);
        await tick();
        dispatch('created', created);
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed to save pipeline');
    } finally {
      submitting = false;
    }
  }

  async function handleDelete() {
    if (!pipeline || !confirm('Delete this pipeline?')) return;

    try {
      await pipelinesStore.delete(pipeline.id);
      dispatch('deleted');
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed to delete pipeline');
    }
  }

  function confirmClose(): boolean {
    if (!hasChanges) return true;
    return confirm('You have unsaved changes. Are you sure you want to close?');
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      if (confirmClose()) dispatch('close');
    }
  }

  function handleBackdropClick() {
    if (confirmClose()) dispatch('close');
  }

  function handleCloseButton() {
    if (confirmClose()) dispatch('close');
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="modal-backdrop" on:click={handleBackdropClick} role="dialog" aria-modal="true">
  <div class="modal" on:click|stopPropagation role="document">
    <form on:submit|preventDefault={handleSubmit}>
      <header class="modal-header">
        <h2>{isEdit ? 'Edit Pipeline' : 'New Pipeline'}</h2>
        <button type="button" class="close-btn" on:click={handleCloseButton}>✕</button>
      </header>

      <div class="modal-body">
        <div class="form-group">
          <label for="name">Name</label>
          <input
            id="name"
            type="text"
            bind:value={name}
            placeholder="e.g., PR Validation"
            required
          />
        </div>

        <div class="form-group">
          <label for="description">Description</label>
          <textarea
            id="description"
            bind:value={description}
            placeholder="What does this pipeline do?"
            rows="2"
          ></textarea>
        </div>

        <div class="steps-section">
          <div class="steps-header">
            <label>Steps ({steps.length})</label>
            <button type="button" class="btn-add-step" on:click={addStep}>+ Add Step</button>
          </div>

          {#if steps.length === 0}
            <p class="no-steps">Add steps to your pipeline</p>
          {:else}
            <div class="steps-list">
              {#each steps as step, index}
                <div class="step-card">
                  <div class="step-header">
                    <span class="step-number">{index + 1}</span>
                    <input
                      type="text"
                      class="step-name-input"
                      bind:value={step.name}
                      placeholder="Step name"
                    />
                    <div class="step-controls">
                      <button
                        type="button"
                        class="btn-icon"
                        title="Move up"
                        disabled={index === 0}
                        on:click={() => moveStep(index, 'up')}
                      >↑</button>
                      <button
                        type="button"
                        class="btn-icon"
                        title="Move down"
                        disabled={index === steps.length - 1}
                        on:click={() => moveStep(index, 'down')}
                      >↓</button>
                      <button
                        type="button"
                        class="btn-icon btn-delete"
                        title="Remove"
                        on:click={() => removeStep(index)}
                      >✕</button>
                    </div>
                  </div>

                  <div class="step-type-selector">
                    {#each ['script', 'docker', 'agent'] as type}
                      <button
                        type="button"
                        class="step-type-btn"
                        class:selected={step.type === type}
                        on:click={() => updateStepType(index, type as StepType)}
                      >
                        {type}
                      </button>
                    {/each}
                  </div>

                  <div class="step-config">
                    {#if step.type === 'script'}
                      <div class="form-group">
                        <label>Script</label>
                        <textarea
                          class="script-input"
                          placeholder={'npm install\nnpm test'}
                          rows="3"
                          value={step.config.command || ''}
                          on:input={(e) => updateStep(index, { ...step, config: { ...step.config, command: e.currentTarget.value }})}
                        ></textarea>
                      </div>
                    {:else if step.type === 'docker'}
                      <div class="form-group">
                        <label>Image</label>
                        <input
                          type="text"
                          placeholder="node:20"
                          value={step.config.image || ''}
                          on:input={(e) => updateStep(index, { ...step, config: { ...step.config, image: e.currentTarget.value }})}
                        />
                      </div>
                      <div class="form-group">
                        <label>Command</label>
                        <textarea
                          class="script-input"
                          placeholder={'npm install\nnpm run build'}
                          rows="2"
                          value={step.config.command || ''}
                          on:input={(e) => updateStep(index, { ...step, config: { ...step.config, command: e.currentTarget.value }})}
                        ></textarea>
                      </div>
                    {:else if step.type === 'agent'}
                      <div class="form-group">
                        <label>Title</label>
                        <input
                          type="text"
                          placeholder="Fix lint errors"
                          value={step.config.title || ''}
                          on:input={(e) => updateStep(index, { ...step, config: { ...step.config, title: e.currentTarget.value }})}
                        />
                      </div>
                      <div class="form-group">
                        <label>Description</label>
                        <textarea
                          placeholder="Instructions for the AI agent"
                          rows="2"
                          value={step.config.description || ''}
                          on:input={(e) => updateStep(index, { ...step, config: { ...step.config, description: e.currentTarget.value }})}
                        ></textarea>
                      </div>
                    {/if}
                  </div>

                  <div class="step-branching">
                    <div class="form-group">
                      <label>On Success</label>
                      <div class="action-selector">
                        <select
                          value={step.on_success.startsWith('merge:') ? 'merge' : step.on_success}
                          on:change={(e) => {
                            const val = e.currentTarget.value;
                            if (val === 'merge') {
                              updateStep(index, { ...step, on_success: 'merge:main' });
                            } else {
                              updateStep(index, { ...step, on_success: val });
                            }
                          }}
                        >
                          <option value="next">Continue to next step</option>
                          <option value="stop">Stop pipeline (success)</option>
                          <option value="merge">Merge to branch...</option>
                        </select>
                        {#if step.on_success.startsWith('merge:')}
                          <input
                            type="text"
                            class="branch-input"
                            placeholder="branch name"
                            value={step.on_success.slice(6)}
                            on:input={(e) => updateStep(index, { ...step, on_success: `merge:${e.currentTarget.value}` })}
                          />
                        {/if}
                      </div>
                    </div>
                    <div class="form-group">
                      <label>On Failure</label>
                      <select
                        value={step.on_failure}
                        on:change={(e) => updateStep(index, { ...step, on_failure: e.currentTarget.value })}
                      >
                        <option value="stop">Stop pipeline (failure)</option>
                        <option value="next">Continue anyway</option>
                      </select>
                    </div>
                  </div>

                  {#if index < steps.length - 1}
                    <div class="step-context-option">
                      <label class="checkbox-label">
                        <input
                          type="checkbox"
                          checked={step.continue_in_context || false}
                          on:change={(e) => updateStep(index, { ...step, continue_in_context: e.currentTarget.checked })}
                        />
                        <span class="checkbox-text">Continue in same context</span>
                        <span class="checkbox-hint">Next step shares workspace & sees this step's output</span>
                      </label>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>
      </div>

      <footer class="modal-footer">
        {#if isEdit && pipeline}
          <div class="action-group-left">
            <button type="button" class="btn-delete" on:click={handleDelete}>Delete</button>
          </div>
        {/if}
        <div class="action-group-right">
          <button type="button" class="btn-secondary" on:click={handleCloseButton}>
            Cancel
          </button>
          <button type="submit" class="btn-primary" disabled={submitting || !name.trim() || steps.length === 0}>
            {submitting ? 'Saving...' : (isEdit ? 'Save' : 'Create Pipeline')}
          </button>
        </div>
      </footer>
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
    z-index: 1000;
    padding: 2rem;
  }

  .modal {
    background: var(--surface-color);
    border-radius: 12px;
    width: 100%;
    max-width: 700px;
    max-height: 90vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
  }

  .modal form {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    height: 100%;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.25rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
  }

  .close-btn {
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 1.25rem;
    cursor: pointer;
    padding: 0.25rem;
  }

  .close-btn:hover {
    color: var(--text-color);
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-group:last-child {
    margin-bottom: 0;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.4rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .form-group input,
  .form-group textarea,
  .form-group select {
    width: 100%;
    padding: 0.6rem 0.75rem;
    background: var(--input-bg);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.9rem;
  }

  .form-group input:focus,
  .form-group textarea:focus,
  .form-group select:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .steps-section {
    margin-top: 1.5rem;
  }

  .steps-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .steps-header label {
    font-weight: 600;
    color: var(--text-color);
  }

  .btn-add-step {
    padding: 0.4rem 0.75rem;
    background: var(--primary-color);
    color: var(--primary-text);
    border: none;
    border-radius: 4px;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .no-steps {
    color: var(--text-muted);
    font-size: 0.9rem;
    text-align: center;
    padding: 2rem;
    background: var(--surface-alt);
    border-radius: 8px;
  }

  .steps-list {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .step-card {
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
  }

  .step-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }

  .step-number {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 1.5rem;
    height: 1.5rem;
    background: var(--primary-color);
    color: var(--primary-text);
    border-radius: 50%;
    font-size: 0.8rem;
    font-weight: 600;
  }

  .step-name-input {
    flex: 1;
    padding: 0.4rem 0.6rem !important;
    font-weight: 500;
  }

  .step-controls {
    display: flex;
    gap: 0.25rem;
  }

  .btn-icon {
    padding: 0.3rem 0.5rem;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 0.85rem;
  }

  .btn-icon:hover:not(:disabled) {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .btn-icon:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }

  .btn-icon.btn-delete:hover:not(:disabled) {
    background: var(--error-color);
    color: white;
    border-color: var(--error-color);
  }

  .step-type-selector {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }

  .step-type-btn {
    flex: 1;
    padding: 0.4rem 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 0.85rem;
    text-transform: capitalize;
  }

  .step-type-btn.selected {
    background: var(--primary-color);
    color: var(--primary-text);
    border-color: var(--primary-color);
  }

  .step-config {
    margin-bottom: 0.75rem;
  }

  .step-config .form-group {
    margin-bottom: 0.5rem;
  }

  .step-branching {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px dashed var(--border-color);
  }

  .step-branching .form-group {
    margin-bottom: 0;
  }

  .action-selector {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .branch-input {
    padding: 0.5rem 0.75rem !important;
    font-size: 0.85rem !important;
    font-family: monospace;
  }

  .modal-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-top: 1px solid var(--border-color);
    background: var(--surface-alt);
  }

  .action-group-left,
  .action-group-right {
    display: flex;
    gap: 0.75rem;
  }

  .action-group-right {
    margin-left: auto;
  }

  .btn-primary,
  .btn-secondary,
  .btn-delete {
    padding: 0.6rem 1.25rem;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    border: none;
  }

  .btn-primary {
    background: var(--primary-color);
    color: var(--primary-text);
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-secondary {
    background: transparent;
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .btn-secondary:hover {
    background: var(--hover-color);
  }

  .btn-delete {
    background: transparent;
    border: 1px solid var(--error-color);
    color: var(--error-color);
  }

  .btn-delete:hover {
    background: var(--error-color);
    color: white;
  }

  /* Script/command textarea styling */
  .script-input {
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;
    font-size: 0.85rem;
    line-height: 1.4;
    resize: vertical;
    min-height: 50px;
  }

  /* Context continuation checkbox */
  .step-context-option {
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px dashed var(--border-color);
  }

  .checkbox-label {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    cursor: pointer;
  }

  .checkbox-label input[type="checkbox"] {
    margin-top: 0.15rem;
    width: 16px;
    height: 16px;
    cursor: pointer;
  }

  .checkbox-text {
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .checkbox-hint {
    display: block;
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 0.15rem;
  }
</style>
