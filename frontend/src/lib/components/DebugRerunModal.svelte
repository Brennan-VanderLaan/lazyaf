<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { PipelineRun, Pipeline, PipelineStepConfig, DebugRerunResponse } from '../api/types';
  import { debug as debugApi } from '../api/client';

  export let run: PipelineRun;
  export let pipeline: Pipeline;

  const dispatch = createEventDispatcher<{
    close: void;
    started: { sessionId: string; token: string; runId: string };
  }>();

  let selectedBreakpoints: Set<number> = new Set();
  let useOriginalCommit = true;
  let customBranch = '';
  let customCommitSha = '';
  let isLoading = false;
  let error: string | null = null;

  function toggleBreakpoint(index: number) {
    if (selectedBreakpoints.has(index)) {
      selectedBreakpoints.delete(index);
    } else {
      selectedBreakpoints.add(index);
    }
    selectedBreakpoints = selectedBreakpoints; // Trigger reactivity
  }

  function selectAll() {
    pipeline.steps.forEach((_, i) => selectedBreakpoints.add(i));
    selectedBreakpoints = selectedBreakpoints;
  }

  function selectNone() {
    selectedBreakpoints.clear();
    selectedBreakpoints = selectedBreakpoints;
  }

  async function startDebugRun() {
    isLoading = true;
    error = null;

    try {
      const response = await debugApi.createRerun(run.id, {
        breakpoints: Array.from(selectedBreakpoints).sort((a, b) => a - b),
        use_original_commit: useOriginalCommit,
        commit_sha: useOriginalCommit ? undefined : customCommitSha || undefined,
        branch: useOriginalCommit ? undefined : customBranch || undefined,
      });

      dispatch('started', {
        sessionId: response.debug_session_id,
        token: response.token,
        runId: response.run_id,
      });
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to start debug run';
    } finally {
      isLoading = false;
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') dispatch('close');
  }

  function handleBackdropClick() {
    dispatch('close');
  }

  function getStepTypeIcon(step: PipelineStepConfig): string {
    switch (step.type) {
      case 'script': return '>';
      case 'docker': return '#';
      case 'agent': return '@';
      default: return '?';
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="modal-backdrop" on:click={handleBackdropClick}>
  <div class="modal" data-testid="debug-rerun-modal" on:click|stopPropagation>
    <div class="header">
      <h2>Debug Re-run</h2>
      <button class="close-btn" on:click={() => dispatch('close')}>x</button>
    </div>

    <div class="content">
      <p class="description">
        Select breakpoints to pause execution before specific steps. When a breakpoint is hit,
        you can connect via CLI to inspect the workspace and debug interactively.
      </p>

      <div class="section">
        <div class="section-header">
          <h3>Breakpoints</h3>
          <div class="section-actions">
            <button class="link-btn" on:click={selectAll}>Select All</button>
            <span class="separator">|</span>
            <button class="link-btn" on:click={selectNone}>Clear</button>
          </div>
        </div>

        <div class="steps-list" data-testid="breakpoint-list">
          {#each pipeline.steps as step, index}
            <label class="step-item" data-testid="breakpoint-item" data-step-index={index} class:selected={selectedBreakpoints.has(index)}>
              <input
                type="checkbox"
                checked={selectedBreakpoints.has(index)}
                on:change={() => toggleBreakpoint(index)}
              />
              <span class="step-icon">{getStepTypeIcon(step)}</span>
              <span class="step-name">{step.name}</span>
              <span class="step-type">{step.type}</span>
            </label>
          {/each}
        </div>

        {#if selectedBreakpoints.size === 0}
          <p class="hint">No breakpoints selected. Pipeline will run to completion.</p>
        {:else}
          <p class="hint">{selectedBreakpoints.size} breakpoint(s) selected.</p>
        {/if}
      </div>

      <div class="section">
        <h3>Commit</h3>
        <div class="commit-options">
          <label class="radio-option">
            <input type="radio" bind:group={useOriginalCommit} value={true} />
            <span>Same as failed run</span>
            {#if run.trigger_context?.commit_sha}
              <code>{run.trigger_context.commit_sha.substring(0, 8)}</code>
            {/if}
          </label>
          <label class="radio-option">
            <input type="radio" bind:group={useOriginalCommit} value={false} />
            <span>Different commit</span>
          </label>
        </div>

        {#if !useOriginalCommit}
          <div class="custom-commit">
            <input
              type="text"
              placeholder="Branch name (optional)"
              bind:value={customBranch}
            />
            <input
              type="text"
              placeholder="Commit SHA (optional)"
              bind:value={customCommitSha}
            />
          </div>
        {/if}
      </div>

      {#if error}
        <div class="error">{error}</div>
      {/if}
    </div>

    <div class="footer">
      <button class="cancel-btn" on:click={() => dispatch('close')} disabled={isLoading}>
        Cancel
      </button>
      <button class="start-btn" data-testid="start-debug-btn" on:click={startDebugRun} disabled={isLoading}>
        {isLoading ? 'Starting...' : 'Start Debug Run'}
      </button>
    </div>
  </div>
</div>

<style>
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: var(--bg-color, #1e1e1e);
    border: 1px solid var(--border-color, #3e3e3e);
    border-radius: 8px;
    width: 90%;
    max-width: 500px;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px;
    border-bottom: 1px solid var(--border-color, #3e3e3e);
  }

  .header h2 {
    margin: 0;
    font-size: 18px;
  }

  .close-btn {
    background: none;
    border: none;
    color: var(--text-color, #ccc);
    font-size: 20px;
    cursor: pointer;
    padding: 4px 8px;
  }

  .close-btn:hover {
    color: var(--text-primary, #fff);
  }

  .content {
    padding: 16px;
    overflow-y: auto;
    flex: 1;
  }

  .description {
    color: var(--text-secondary, #999);
    font-size: 14px;
    margin-bottom: 16px;
  }

  .section {
    margin-bottom: 20px;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }

  .section h3 {
    margin: 0;
    font-size: 14px;
    color: var(--text-secondary, #999);
  }

  .section-actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .link-btn {
    background: none;
    border: none;
    color: var(--accent-color, #007acc);
    cursor: pointer;
    padding: 0;
    font-size: 12px;
  }

  .link-btn:hover {
    text-decoration: underline;
  }

  .separator {
    color: var(--text-secondary, #666);
  }

  .steps-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 200px;
    overflow-y: auto;
    border: 1px solid var(--border-color, #3e3e3e);
    border-radius: 4px;
    padding: 8px;
  }

  .step-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px;
    border-radius: 4px;
    cursor: pointer;
  }

  .step-item:hover {
    background: var(--hover-bg, #2a2a2a);
  }

  .step-item.selected {
    background: var(--selected-bg, #1a3a4a);
  }

  .step-icon {
    font-family: monospace;
    color: var(--accent-color, #007acc);
    width: 16px;
    text-align: center;
  }

  .step-name {
    flex: 1;
  }

  .step-type {
    color: var(--text-secondary, #666);
    font-size: 12px;
  }

  .hint {
    color: var(--text-secondary, #666);
    font-size: 12px;
    margin-top: 8px;
  }

  .commit-options {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .radio-option {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
  }

  .radio-option code {
    background: var(--code-bg, #2a2a2a);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 12px;
  }

  .custom-commit {
    margin-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-left: 24px;
  }

  .custom-commit input {
    background: var(--input-bg, #2a2a2a);
    border: 1px solid var(--border-color, #3e3e3e);
    border-radius: 4px;
    padding: 8px;
    color: var(--text-color, #ccc);
  }

  .error {
    background: var(--error-bg, #3a2020);
    color: var(--error-color, #ff6b6b);
    padding: 8px 12px;
    border-radius: 4px;
    font-size: 14px;
  }

  .footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    padding: 16px;
    border-top: 1px solid var(--border-color, #3e3e3e);
  }

  .cancel-btn, .start-btn {
    padding: 8px 16px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
  }

  .cancel-btn {
    background: none;
    border: 1px solid var(--border-color, #3e3e3e);
    color: var(--text-color, #ccc);
  }

  .cancel-btn:hover:not(:disabled) {
    background: var(--hover-bg, #2a2a2a);
  }

  .start-btn {
    background: var(--accent-color, #007acc);
    border: none;
    color: white;
  }

  .start-btn:hover:not(:disabled) {
    background: var(--accent-hover, #0096e0);
  }

  .start-btn:disabled, .cancel-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
