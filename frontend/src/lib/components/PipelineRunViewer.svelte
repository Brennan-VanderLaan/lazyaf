<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import type { PipelineRun, StepRun, RunStatus, StepLogsResponse, Pipeline } from '../api/types';
  import { activeRunsStore } from '../stores/pipelines';
  import { pipelineRuns as runsApi, pipelines as pipelinesApi } from '../api/client';
  import DebugRerunModal from './DebugRerunModal.svelte';
  import DebugPanel from './DebugPanel.svelte';

  export let run: PipelineRun;

  const dispatch = createEventDispatcher<{
    close: void;
  }>();

  let selectedStepIndex: number | null = null;
  let stepLogs: StepLogsResponse | null = null;
  let loadingLogs = false;
  let refreshInterval: ReturnType<typeof setInterval> | null = null;

  // Debug state
  let pipeline: Pipeline | null = null;
  let showDebugModal = false;
  let debugSessionId: string | null = null;
  let debugToken: string | null = null;

  // Load pipeline data for debug modal
  $: canDebug = run.status === 'failed' || run.status === 'cancelled';

  onMount(async () => {
    // Load pipeline for debug modal
    try {
      pipeline = await pipelinesApi.get(run.pipeline_id);
    } catch (e) {
      // Pipeline may not exist, that's okay
    }
  });

  // Auto-refresh while running
  $: if (run.status === 'running' || run.status === 'pending') {
    if (!refreshInterval) {
      refreshInterval = setInterval(async () => {
        try {
          const updated = await runsApi.get(run.id);
          activeRunsStore.updateRun(updated);
          run = updated;
          // Refresh logs for selected step
          if (selectedStepIndex !== null) {
            await loadStepLogs(selectedStepIndex);
          }
        } catch (e) {
          // Ignore errors during refresh
        }
      }, 2000);
    }
  } else if (refreshInterval) {
    clearInterval(refreshInterval);
    refreshInterval = null;
  }

  onDestroy(() => {
    if (refreshInterval) {
      clearInterval(refreshInterval);
    }
  });

  async function loadStepLogs(stepIndex: number) {
    if (loadingLogs) return;
    loadingLogs = true;
    try {
      stepLogs = await runsApi.stepLogs(run.id, stepIndex);
    } catch (e) {
      stepLogs = null;
    } finally {
      loadingLogs = false;
    }
  }

  function selectStep(index: number) {
    selectedStepIndex = index;
    loadStepLogs(index);
  }

  async function handleCancel() {
    try {
      const cancelled = await activeRunsStore.cancel(run.id);
      run = cancelled;
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed to cancel pipeline');
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') dispatch('close');
  }

  function handleBackdropClick() {
    dispatch('close');
  }

  function getStatusColor(status: RunStatus): string {
    switch (status) {
      case 'pending': return 'var(--text-muted)';
      case 'running': return 'var(--warning-color)';
      case 'passed': return 'var(--success-color)';
      case 'failed': return 'var(--error-color)';
      case 'cancelled': return 'var(--text-muted)';
      default: return 'var(--text-muted)';
    }
  }

  function getStatusIcon(status: RunStatus): string {
    switch (status) {
      case 'pending': return '◯';
      case 'running': return '⟳';
      case 'passed': return '✓';
      case 'failed': return '✗';
      case 'cancelled': return '⊘';
      default: return '?';
    }
  }

  function formatDuration(start: string | null, end: string | null): string {
    if (!start) return '-';
    const startTime = new Date(start).getTime();
    const endTime = end ? new Date(end).getTime() : Date.now();
    const seconds = Math.floor((endTime - startTime) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${seconds % 60}s`;
  }

  function handleDebugStarted(event: CustomEvent<{ sessionId: string; token: string; runId: string }>) {
    showDebugModal = false;
    debugSessionId = event.detail.sessionId;
    debugToken = event.detail.token;
  }

  function handleDebugClose() {
    debugSessionId = null;
    debugToken = null;
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="modal-backdrop" on:click={handleBackdropClick} role="dialog" aria-modal="true">
  <div class="modal" data-testid="run-viewer" on:click|stopPropagation role="document">
    <header class="modal-header">
      <div class="header-info">
        <h2>Pipeline Run</h2>
        <span class="run-status" style="color: {getStatusColor(run.status as RunStatus)}">
          {getStatusIcon(run.status as RunStatus)} {run.status}
        </span>
      </div>
      <button type="button" class="close-btn" on:click={() => dispatch('close')}>✕</button>
    </header>

    <div class="modal-body">
      <div class="progress-bar">
        <div
          class="progress-fill"
          style="width: {(run.steps_completed / run.steps_total) * 100}%"
          class:running={run.status === 'running'}
          class:passed={run.status === 'passed'}
          class:failed={run.status === 'failed'}
        ></div>
        <span class="progress-text">{run.steps_completed} / {run.steps_total} steps</span>
      </div>

      <div class="run-meta">
        <span>Trigger: {run.trigger_type}</span>
        <span>Duration: {formatDuration(run.started_at, run.completed_at)}</span>
      </div>

      <div class="steps-timeline" data-testid="steps">
        {#each run.step_runs || [] as stepRun, index}
          <button
            class="step-item"
            data-testid="step"
            data-step-index={index}
            data-status={stepRun.status}
            class:selected={selectedStepIndex === index}
            class:current={run.current_step === index && run.status === 'running'}
            on:click={() => selectStep(index)}
          >
            <span class="step-status" style="color: {getStatusColor(stepRun.status as RunStatus)}">
              {getStatusIcon(stepRun.status as RunStatus)}
            </span>
            <span class="step-info">
              <span class="step-name">{stepRun.step_name}</span>
              <span class="step-duration">{formatDuration(stepRun.started_at, stepRun.completed_at)}</span>
            </span>
          </button>
        {/each}
      </div>

      {#if selectedStepIndex !== null}
        <div class="step-details">
          <div class="step-details-header">
            <h3>{run.step_runs?.[selectedStepIndex]?.step_name || `Step ${selectedStepIndex + 1}`}</h3>
            {#if stepLogs?.error}
              <span class="error-badge">Error</span>
            {/if}
          </div>

          {#if loadingLogs}
            <div class="loading">Loading logs...</div>
          {:else if stepLogs}
            {#if stepLogs.error}
              <div class="error-message">{stepLogs.error}</div>
            {/if}
            <pre class="logs" data-testid="logs">{stepLogs.logs || '(No logs)'}</pre>
          {:else}
            <p class="no-logs">Select a step to view logs</p>
          {/if}
        </div>
      {/if}
    </div>

    <footer class="modal-footer">
      {#if run.status === 'running' || run.status === 'pending'}
        <button type="button" class="btn-cancel" on:click={handleCancel}>Cancel Pipeline</button>
      {/if}
      {#if canDebug && pipeline}
        <button type="button" class="btn-debug" on:click={() => showDebugModal = true}>
          Debug Re-run
        </button>
      {/if}
      <button type="button" class="btn-secondary" on:click={() => dispatch('close')}>
        Close
      </button>
    </footer>

    {#if debugSessionId && debugToken}
      <div class="debug-panel-container">
        <DebugPanel
          sessionId={debugSessionId}
          token={debugToken}
          on:close={handleDebugClose}
          on:resumed={() => { /* Refresh run state */ }}
          on:aborted={() => { /* Refresh run state */ }}
        />
      </div>
    {/if}
  </div>
</div>

{#if showDebugModal && pipeline}
  <DebugRerunModal
    {run}
    {pipeline}
    on:close={() => showDebugModal = false}
    on:started={handleDebugStarted}
  />
{/if}

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
    max-width: 800px;
    max-height: 90vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.25rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .header-info {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
  }

  .run-status {
    font-weight: 600;
    text-transform: uppercase;
    font-size: 0.85rem;
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

  .progress-bar {
    position: relative;
    height: 24px;
    background: var(--surface-alt);
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 1rem;
  }

  .progress-fill {
    height: 100%;
    background: var(--primary-color);
    transition: width 0.3s ease;
  }

  .progress-fill.running {
    background: var(--warning-color);
  }

  .progress-fill.passed {
    background: var(--success-color);
  }

  .progress-fill.failed {
    background: var(--error-color);
  }

  .progress-text {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .run-meta {
    display: flex;
    gap: 1.5rem;
    font-size: 0.85rem;
    color: var(--text-muted);
    margin-bottom: 1.5rem;
  }

  .steps-timeline {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-bottom: 1.5rem;
  }

  .step-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    color: var(--text-color);
    width: 100%;
  }

  .step-item:hover {
    background: var(--hover-color);
  }

  .step-item.selected {
    border-color: var(--primary-color);
  }

  .step-item.current {
    border-color: var(--warning-color);
  }

  .step-status {
    font-size: 1rem;
    font-weight: bold;
  }

  .step-info {
    flex: 1;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .step-name {
    font-weight: 500;
  }

  .step-duration {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-family: monospace;
  }

  .step-details {
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
  }

  .step-details-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .step-details-header h3 {
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
  }

  .error-badge {
    padding: 0.2rem 0.5rem;
    background: var(--error-color);
    color: white;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
  }

  .loading {
    color: var(--text-muted);
    font-style: italic;
  }

  .error-message {
    padding: 0.75rem;
    background: rgba(243, 139, 168, 0.1);
    border: 1px solid var(--error-color);
    border-radius: 6px;
    color: var(--error-color);
    font-size: 0.9rem;
    margin-bottom: 0.75rem;
  }

  .logs {
    background: var(--bg-color);
    padding: 1rem;
    border-radius: 6px;
    font-family: monospace;
    font-size: 0.8rem;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 300px;
    overflow-y: auto;
    margin: 0;
  }

  .no-logs {
    color: var(--text-muted);
    font-style: italic;
    margin: 0;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    padding: 1rem 1.5rem;
    border-top: 1px solid var(--border-color);
    background: var(--surface-alt);
  }

  .btn-secondary,
  .btn-cancel {
    padding: 0.6rem 1.25rem;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    border: none;
  }

  .btn-secondary {
    background: transparent;
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .btn-secondary:hover {
    background: var(--hover-color);
  }

  .btn-cancel {
    background: transparent;
    border: 1px solid var(--error-color);
    color: var(--error-color);
  }

  .btn-cancel:hover {
    background: var(--error-color);
    color: white;
  }

  .btn-debug {
    background: var(--primary-color);
    color: white;
  }

  .btn-debug:hover {
    background: var(--primary-hover);
  }

  .debug-panel-container {
    margin-top: 1rem;
    border-top: 1px solid var(--border-color);
    padding-top: 1rem;
  }
</style>
