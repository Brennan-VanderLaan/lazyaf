<script lang="ts">
  import type { PipelineStepV2, StepType, RunnerType } from '../../api/types';
  import { EndpointSelect } from '../endpoint';

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
    // M14: the LazyAF harness driving a self-hosted OpenAI-compatible
    // endpoint. Choosing it swaps the model field for an endpoint picker,
    // because a self-hosted step names an ENDPOINT rather than a model id.
    { value: 'openai-harness', label: 'Self-hosted (openai-harness)' },
    { value: 'any', label: 'Any Available' },
  ];

  /** True while the step is aimed at a model endpoint rather than a CLI. */
  let isHarness = $derived(editedStep.config.runner_type === 'openai-harness');

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
  <div class="modal" data-testid="step-config-modal" onclick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
    <div class="modal-header">
      <h2>{isNew ? 'Add New Step' : 'Edit Step'}</h2>
      <button class="close-btn" data-testid="step-config-close-btn" onclick={onCancel} title="Close">&times;</button>
    </div>

    <div class="modal-body">
      <!-- Step Name -->
      <div class="form-group">
        <label for="step-name">Step Name</label>
        <input
          id="step-name"
          name="step-name"
          data-testid="step-name-input"
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
              data-testid="step-type-{type.value}"
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
            name="script-command"
            data-testid="script-command-input"
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
            name="docker-image"
            data-testid="docker-image-input"
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
            name="docker-command"
            data-testid="docker-command-input"
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

        {#if isHarness}
          <div class="form-group">
            <label for="agent-endpoint">Model endpoint</label>
            <EndpointSelect
              id="agent-endpoint"
              testid="step-endpoint-select"
              value={editedStep.config.model ?? ''}
              onChange={(value) => (editedStep.config.model = value || undefined)}
            />
            <p class="hint">
              Emitted as <code>model: "endpoint:&lt;name&gt;"</code>. There is no default
              endpoint — guessing which GPU to bill is not a recoverable mistake.
            </p>
          </div>

          <details class="harness-budgets" data-testid="harness-budgets">
            <summary>Harness budgets</summary>
            <div class="form-row">
              <div class="form-group half">
                <label for="harness-mode">Loop mode</label>
                <select
                  id="harness-mode"
                  data-testid="harness-mode-select"
                  value={editedStep.config.harness?.mode ?? 'auto'}
                  onchange={(e) => {
                    const mode = e.currentTarget.value as 'auto' | 'tools' | 'text';
                    editedStep.config.harness = { ...editedStep.config.harness, mode };
                  }}
                >
                  <option value="auto">auto (decide from the probe)</option>
                  <option value="tools">tools (pin)</option>
                  <option value="text">text (pin the fallback protocol)</option>
                </select>
              </div>
              <div class="form-group half">
                <label for="harness-iterations">Max turns</label>
                <input
                  id="harness-iterations"
                  type="number"
                  min="1"
                  data-testid="harness-iterations-input"
                  placeholder="40"
                  value={editedStep.config.harness?.max_iterations ?? ''}
                  oninput={(e) => {
                    const raw = e.currentTarget.value.trim();
                    editedStep.config.harness = {
                      ...editedStep.config.harness,
                      max_iterations: raw === '' ? undefined : Number(raw),
                    };
                  }}
                />
              </div>
            </div>
            <p class="hint">
              Blank takes the backend default. Pinning <code>text</code> on a tool-capable model
              is how an experiment makes the loop shape an independent variable.
            </p>
          </details>
        {/if}

        <div class="form-group">
          <label for="agent-title">Task Title</label>
          <input
            id="agent-title"
            name="agent-title"
            data-testid="agent-title-input"
            type="text"
            bind:value={editedStep.config.title}
            placeholder="e.g., Fix failing tests"
          />
        </div>

        <div class="form-group">
          <label for="agent-desc">Task Description</label>
          <textarea
            id="agent-desc"
            name="agent-desc"
            data-testid="agent-desc-input"
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
      <button class="btn secondary" data-testid="step-config-cancel-btn" onclick={onCancel}>Cancel</button>
      <button class="btn primary" data-testid="step-config-save-btn" onclick={handleSave}>
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

  /* M14: the self-hosted endpoint picker and its collapsed budgets. */
  .hint {
    margin: 0.3rem 0 0;
    font-size: 0.72rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  .harness-budgets {
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 0.5rem 0.75rem;
    margin-bottom: 1rem;
  }

  .harness-budgets summary {
    cursor: pointer;
    font-size: 0.82rem;
    color: var(--text-muted);
  }

  .harness-budgets code,
  .hint code {
    font-size: 0.7rem;
    background: var(--badge-bg);
    padding: 0 0.2rem;
    border-radius: 3px;
  }
</style>
