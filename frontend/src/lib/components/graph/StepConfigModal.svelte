<script lang="ts">
  import type { PipelineStepV2, StepType, RunnerType } from '../../api/types';

  interface Props {
    step: PipelineStepV2;
    isNew: boolean;
    onSave: (step: PipelineStepV2) => void;
    onCancel: () => void;
  }

  let { step, isNew, onSave, onCancel }: Props = $props();

  // Create editable copy
  let editedStep = $state<PipelineStepV2>({ ...step, config: { ...step.config } });

  // Step type options
  const stepTypes: { value: StepType; label: string; icon: string }[] = [
    { value: 'script', label: 'Script', icon: '>_' },
    { value: 'docker', label: 'Docker', icon: '[]' },
    { value: 'agent', label: 'AI Agent', icon: '*' },
  ];

  // Runner type options for agent steps
  const runnerTypes: { value: RunnerType; label: string }[] = [
    { value: 'claude-code', label: 'Claude Code' },
    { value: 'gemini', label: 'Gemini' },
    { value: 'any', label: 'Any Available' },
  ];

  // Update config when type changes
  function onTypeChange(newType: StepType) {
    editedStep.type = newType;
    // Reset config for new type
    if (newType === 'script') {
      editedStep.config = { command: '' };
    } else if (newType === 'docker') {
      editedStep.config = { image: 'ubuntu:latest', command: '' };
    } else if (newType === 'agent') {
      editedStep.config = { runner_type: 'claude-code', title: '', description: '' };
    }
  }

  function handleSave() {
    // Validate required fields
    if (!editedStep.name.trim()) {
      alert('Step name is required');
      return;
    }

    if (editedStep.type === 'script' && !editedStep.config.command?.trim()) {
      alert('Command is required for script steps');
      return;
    }

    if (editedStep.type === 'docker' && !editedStep.config.image?.trim()) {
      alert('Image is required for Docker steps');
      return;
    }

    if (editedStep.type === 'agent' && !editedStep.config.title?.trim()) {
      alert('Title is required for agent steps');
      return;
    }

    onSave(editedStep);
  }

  // Handle escape to cancel
  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      onCancel();
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="modal-backdrop" onclick={onCancel} role="presentation">
  <div class="modal" onclick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
    <div class="modal-header">
      <h2>{isNew ? 'Add New Step' : 'Edit Step'}</h2>
      <button class="close-btn" onclick={onCancel} title="Close">&times;</button>
    </div>

    <div class="modal-body">
      <!-- Step Name -->
      <div class="form-group">
        <label for="step-name">Step Name</label>
        <input
          id="step-name"
          type="text"
          bind:value={editedStep.name}
          placeholder="e.g., Build, Test, Deploy"
        />
      </div>

      <!-- Step Type -->
      <div class="form-group">
        <label>Step Type</label>
        <div class="type-selector">
          {#each stepTypes as type}
            <button
              class="type-btn"
              class:selected={editedStep.type === type.value}
              onclick={() => onTypeChange(type.value)}
            >
              <span class="type-icon">{type.icon}</span>
              <span class="type-label">{type.label}</span>
            </button>
          {/each}
        </div>
      </div>

      <!-- Type-specific config -->
      {#if editedStep.type === 'script'}
        <div class="form-group">
          <label for="script-command">Command</label>
          <input
            id="script-command"
            type="text"
            bind:value={editedStep.config.command}
            placeholder="e.g., npm run build"
            class="mono"
          />
        </div>

        <div class="form-group">
          <label for="script-workdir">Working Directory (optional)</label>
          <input
            id="script-workdir"
            type="text"
            bind:value={editedStep.config.working_dir}
            placeholder="e.g., ./packages/frontend"
            class="mono"
          />
        </div>
      {/if}

      {#if editedStep.type === 'docker'}
        <div class="form-group">
          <label for="docker-image">Docker Image</label>
          <input
            id="docker-image"
            type="text"
            bind:value={editedStep.config.image}
            placeholder="e.g., node:18, python:3.11"
            class="mono"
          />
        </div>

        <div class="form-group">
          <label for="docker-command">Command</label>
          <input
            id="docker-command"
            type="text"
            bind:value={editedStep.config.command}
            placeholder="e.g., npm test"
            class="mono"
          />
        </div>
      {/if}

      {#if editedStep.type === 'agent'}
        <div class="form-group">
          <label for="agent-runner">Runner Type</label>
          <select id="agent-runner" bind:value={editedStep.config.runner_type}>
            {#each runnerTypes as runner}
              <option value={runner.value}>{runner.label}</option>
            {/each}
          </select>
        </div>

        <div class="form-group">
          <label for="agent-title">Task Title</label>
          <input
            id="agent-title"
            type="text"
            bind:value={editedStep.config.title}
            placeholder="e.g., Fix failing tests"
          />
        </div>

        <div class="form-group">
          <label for="agent-desc">Task Description</label>
          <textarea
            id="agent-desc"
            bind:value={editedStep.config.description}
            placeholder="Describe what the AI agent should do..."
            rows="3"
          ></textarea>
        </div>
      {/if}

      <!-- Common options -->
      <div class="form-divider"></div>

      <div class="form-row">
        <div class="form-group half">
          <label for="step-timeout">Timeout (seconds)</label>
          <input
            id="step-timeout"
            type="number"
            bind:value={editedStep.timeout}
            min="1"
            max="3600"
          />
        </div>

        <div class="form-group half">
          <label class="checkbox-label">
            <input
              type="checkbox"
              bind:checked={editedStep.continue_in_context}
            />
            <span>Continue in same workspace</span>
          </label>
          <span class="help-text">Next step shares files with this one</span>
        </div>
      </div>
    </div>

    <div class="modal-footer">
      <button class="btn secondary" onclick={onCancel}>Cancel</button>
      <button class="btn primary" onclick={handleSave}>
        {isNew ? 'Add Step' : 'Save Changes'}
      </button>
    </div>
  </div>
</div>

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10000;
    animation: fade-in 0.15s ease-out;
  }

  @keyframes fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .modal {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    width: 480px;
    max-width: 90vw;
    max-height: 85vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    animation: modal-appear 0.2s ease-out;
  }

  @keyframes modal-appear {
    from {
      opacity: 0;
      transform: scale(0.95) translateY(10px);
    }
    to {
      opacity: 1;
      transform: scale(1) translateY(0);
    }
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-color);
  }

  .close-btn {
    width: 28px;
    height: 28px;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--text-muted);
    font-size: 20px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .close-btn:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .modal-body {
    flex: 1;
    padding: 20px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .form-group.half {
    flex: 1;
  }

  .form-row {
    display: flex;
    gap: 16px;
  }

  label {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-color);
  }

  input[type="text"],
  input[type="number"],
  select,
  textarea {
    padding: 10px 12px;
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 13px;
  }

  input:focus,
  select:focus,
  textarea:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  input.mono,
  textarea.mono {
    font-family: monospace;
  }

  textarea {
    resize: vertical;
    min-height: 60px;
  }

  select {
    cursor: pointer;
  }

  .type-selector {
    display: flex;
    gap: 8px;
  }

  .type-btn {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    padding: 12px;
    background: var(--surface-alt);
    border: 2px solid var(--border-color);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .type-btn:hover {
    border-color: var(--primary-color);
  }

  .type-btn.selected {
    border-color: var(--primary-color);
    background: rgba(137, 180, 250, 0.1);
  }

  .type-icon {
    font-size: 18px;
    font-family: monospace;
    font-weight: bold;
    color: var(--primary-color);
  }

  .type-label {
    font-size: 12px;
    color: var(--text-color);
  }

  .form-divider {
    height: 1px;
    background: var(--border-color);
    margin: 8px 0;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
  }

  .checkbox-label input[type="checkbox"] {
    width: 16px;
    height: 16px;
    cursor: pointer;
  }

  .checkbox-label span {
    font-weight: normal;
  }

  .help-text {
    font-size: 11px;
    color: var(--text-muted);
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    padding: 16px 20px;
    border-top: 1px solid var(--border-color);
    background: var(--surface-alt);
  }

  .btn {
    padding: 10px 20px;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .btn.secondary {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .btn.secondary:hover {
    background: var(--hover-color);
  }

  .btn.primary {
    background: var(--primary-color);
    color: var(--primary-text);
  }

  .btn.primary:hover {
    filter: brightness(1.1);
  }
</style>
