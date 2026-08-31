<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import type { Pipeline, PipelineRun, StepRun, RunStatus, StepLogsResponse } from '../api/types';
  import { activeRunsStore, liveStepLogsStore, stepLogKey } from '../stores/pipelines';
  import { pipelineRuns as runsApi, pipelines as pipelinesApi } from '../api/client';
  // 12.7 debug re-run. Both components are self-hiding - the panel renders
  // nothing without a session - so the viewer stays exactly what it was for
  // every run that is not being debugged.
  import { DebugPanel, DebugRerunModal } from './debug';
  // T1: this file carried its own copy of the duration maths that rendered
  // `-14399s` off naive-UTC timestamps. There is one copy now.
  import { formatDuration } from '../utils/time';

  export let run: PipelineRun;

  const dispatch = createEventDispatcher<{
    close: void;
  }>();

  // The STEP INDEX of the selected step - the same number the API keys logs
  // by - never its position in `step_runs`. Those two agree only while every
  // step has reported: a parallel or conditional pipeline where step 2
  // reported before step 1 made row #2 fetch step 1's logs, and made a later
  // frame for the missing step re-point the heading at a step the user was
  // not reading. R3: one meaning per number.
  let selectedStepIndex: number | null = null;
  let stepLogs: StepLogsResponse | null = null;
  let loadingLogs = false;
  let logsFetchError: string | null = null;
  let refreshInterval: ReturnType<typeof setInterval> | null = null;

  /**
   * The log body, ONE ENTRY PER LINE.
   *
   * Kept as a growing list rather than recomputed from whichever source is
   * currently fresher: the WS tail and the REST snapshot are two views of the
   * same stream, and swapping between them (the old "longer string wins")
   * rewrote every line in the pane. Adopting only a candidate at least as long
   * as what is on screen means the pane only ever gains lines, so the nodes a
   * user has selected are never touched.
   */
  let displayedLines: string[] = [];

  // The launcher needs the PIPELINE, not just the run: breakpoints are step
  // keys read off the step list. Fetched on demand, so an ordinary viewer
  // open costs no extra request.
  // Ticks once a second so a running step's duration counts up on its own
  // rather than freezing until the next store update happens to re-render.
  let viewerNow = Date.now();

  let showDebugRerun = false;
  let debugPipeline: Pipeline | null = null;
  let debugLaunchError: string | null = null;

  // Live view of the run: WS frames (pipeline_run_status / step_run_status /
  // step_update) land in activeRunsStore, so prefer the store's copy over the
  // prop snapshot. The REST poll below remains as a fallback for missed
  // frames and also feeds the store.
  $: liveRun = $activeRunsStore.get(run.id) ?? run;

  // Rendered in step_index order, and KEYED by it below. `step_runs` is
  // rebuilt by every REST poll and re-sorted whenever a late step reports, so
  // without a stable order and a stable key the rows shuffle under the cursor
  // and the click lands on a step the user did not aim at.
  $: orderedSteps = [...(liveRun.step_runs ?? [])].sort((a, b) => a.step_index - b.step_index);

  $: selectedStep =
    selectedStepIndex === null
      ? null
      : orderedSteps.find(s => s.step_index === selectedStepIndex) ?? null;

  // Live log tail streamed over the WS (step_log / step_log_batch) for the
  // selected step.
  $: liveLines =
    selectedStepIndex !== null
      ? $liveStepLogsStore.get(stepLogKey(run.id, selectedStepIndex)) ?? []
      : [];

  // The persisted snapshot (REST), split once at the component boundary.
  $: snapshotLines = splitLogLines(stepLogs?.logs ?? '');

  $: adoptLogLines(liveLines, snapshotLines);

  // Auto-refresh while running
  $: if (liveRun.status === 'running' || liveRun.status === 'pending') {
    if (!refreshInterval) {
      refreshInterval = setInterval(async () => {
        try {
          const updated = await runsApi.get(run.id);
          activeRunsStore.updateRun(updated);
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

  const clockInterval = setInterval(() => (viewerNow = Date.now()), 1000);

  onDestroy(() => {
    clearInterval(clockInterval);
    if (refreshInterval) {
      clearInterval(refreshInterval);
    }
  });

  function splitLogLines(text: string): string[] {
    if (!text) return [];
    const lines = text.split('\n');
    // A trailing newline terminates the last line, it is not an empty one.
    if (lines[lines.length - 1] === '') lines.pop();
    return lines;
  }

  /**
   * Take whichever source has more lines, but never go backwards. Called from
   * a reactive statement; `displayedLines` is deliberately not referenced in
   * that statement so writing it here cannot re-trigger the block.
   */
  function adoptLogLines(live: string[], snapshot: string[]) {
    const candidate = snapshot.length >= live.length ? snapshot : live;
    if (candidate.length >= displayedLines.length) {
      displayedLines = candidate;
    }
  }

  async function loadStepLogs(stepIndex: number) {
    if (loadingLogs) return;
    loadingLogs = true;
    try {
      stepLogs = await runsApi.stepLogs(run.id, stepIndex);
      logsFetchError = null;
    } catch (e) {
      // R1: say so, and do NOT blank out logs already on screen - a failed
      // 2s refresh used to replace a transcript the user was reading with
      // nothing at all. The last snapshot stands and the WS tail keeps
      // feeding the pane; the banner says the refresh is behind.
      logsFetchError = e instanceof Error ? e.message : 'Failed to refresh logs';
      if (displayedLines.length === 0) stepLogs = null;
    } finally {
      loadingLogs = false;
    }
  }

  /** @param stepIndex the StepRun's `step_index`, not its row position. */
  function selectStep(stepIndex: number) {
    if (selectedStepIndex === stepIndex) return;
    selectedStepIndex = stepIndex;
    stepLogs = null;
    displayedLines = [];
    logsFetchError = null;
    loadStepLogs(stepIndex);
  }

  async function handleCancel() {
    try {
      const cancelled = await activeRunsStore.cancel(run.id);
      run = cancelled;
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed to cancel pipeline');
    }
  }

  async function openDebugRerun() {
    debugLaunchError = null;
    try {
      debugPipeline = await pipelinesApi.get(liveRun.pipeline_id);
      showDebugRerun = true;
    } catch (e) {
      // R1: a launcher that silently does nothing is worse than one that
      // says it could not read the pipeline.
      debugLaunchError =
        e instanceof Error ? e.message : 'Could not load the pipeline definition';
    }
  }

  async function handleDebugStarted(
    event: CustomEvent<{ sessionId: string; runId: string; joinCommand: string }>
  ) {
    showDebugRerun = false;
    // The viewer FOLLOWS the new run. The session gates THAT run, so
    // leaving the viewer on the original would show a panel describing a
    // different pipeline run than the steps underneath it.
    selectedStepIndex = null;
    stepLogs = null;
    displayedLines = [];
    logsFetchError = null;
    try {
      const started = await runsApi.get(event.detail.runId);
      activeRunsStore.updateRun(started);
      run = started;
    } catch (e) {
      debugLaunchError =
        e instanceof Error ? e.message : 'The debug run started but could not be loaded';
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') dispatch('close');
  }

  // `click` fires on the nearest common ancestor of mousedown and mouseup, so
  // a selection drag that STARTS on a log line and ends past the modal edge
  // dispatches click on the backdrop itself - the inner `on:click|stopPropagation`
  // never sees it and cannot help. That closed the viewer mid-drag and took
  // the text with it. Close only when the press AND the release landed on the
  // backdrop.
  let pressedOnBackdrop = false;

  function handleBackdropMouseDown(e: MouseEvent) {
    pressedOnBackdrop = e.target === e.currentTarget;
  }

  function handleBackdropClick(e: MouseEvent) {
    const shouldClose = pressedOnBackdrop && e.target === e.currentTarget;
    pressedOnBackdrop = false;
    if (shouldClose) dispatch('close');
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

</script>

<svelte:window on:keydown={handleKeydown} />

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
  class="modal-backdrop"
  on:mousedown={handleBackdropMouseDown}
  on:click={handleBackdropClick}
  role="dialog"
  aria-modal="true"
>
  <div class="modal" data-testid="run-viewer" on:click|stopPropagation role="document">
    <header class="modal-header">
      <div class="header-info">
        <h2>Pipeline Run</h2>
        <span class="run-status" data-testid="run-status" data-status={liveRun.status} style="color: {getStatusColor(liveRun.status as RunStatus)}">
          {getStatusIcon(liveRun.status as RunStatus)} {liveRun.status}
        </span>
      </div>
      <button type="button" class="close-btn" on:click={() => dispatch('close')}>✕</button>
    </header>

    <div class="modal-body">
      <div class="progress-bar">
        <div
          class="progress-fill"
          style="width: {(liveRun.steps_completed / liveRun.steps_total) * 100}%"
          class:running={liveRun.status === 'running'}
          class:passed={liveRun.status === 'passed'}
          class:failed={liveRun.status === 'failed'}
        ></div>
        <span class="progress-text">{liveRun.steps_completed} / {liveRun.steps_total} steps</span>
      </div>

      <div class="run-meta">
        <span>Trigger: {liveRun.trigger_type}</span>
        <span>Duration: {formatDuration(liveRun.started_at, liveRun.completed_at, viewerNow)}</span>
      </div>

      {#if debugLaunchError}
        <div class="error-message" data-testid="debug-launch-error">{debugLaunchError}</div>
      {/if}

      <DebugPanel run={liveRun} />

      <div class="steps-timeline" data-testid="steps">
        {#each orderedSteps as stepRun (stepRun.step_index)}
          <button
            class="step-item"
            data-testid="step"
            data-step-index={stepRun.step_index}
            data-status={stepRun.status}
            class:selected={selectedStepIndex === stepRun.step_index}
            class:current={liveRun.current_step === stepRun.step_index && liveRun.status === 'running'}
            on:click={() => selectStep(stepRun.step_index)}
          >
            <span class="step-status" style="color: {getStatusColor(stepRun.status as RunStatus)}">
              {getStatusIcon(stepRun.status as RunStatus)}
            </span>
            <span class="step-info">
              <span class="step-name">{stepRun.step_name}</span>
              <span class="step-duration">{formatDuration(stepRun.started_at, stepRun.completed_at, viewerNow)}</span>
            </span>
          </button>
        {/each}
      </div>

      {#if selectedStepIndex !== null}
        <div class="step-details">
          <div class="step-details-header">
            <h3>{selectedStep?.step_name || `Step ${selectedStepIndex + 1}`}</h3>
            {#if stepLogs?.error}
              <span class="error-badge">Error</span>
            {/if}
          </div>

          {#if stepLogs?.error}
            <div class="error-message">{stepLogs.error}</div>
          {/if}
          {#if logsFetchError}
            <div class="logs-stale" data-testid="logs-stale">
              Log refresh failed: {logsFetchError}. Showing the last lines received.
            </div>
          {/if}
          {#if loadingLogs && displayedLines.length === 0}
            <div class="loading">Loading logs...</div>
          {:else}
            <!-- ONE NODE PER LINE, keyed by position. `<pre>{wholeLog}</pre>`
                 is a single text node: every arriving line replaced it and
                 collapsed any selection the user had made, so highlighting a
                 failing assertion to copy it was impossible while the step was
                 still running. Appending a node leaves the earlier ones - and
                 the selection over them - alone. -->
            <div class="logs" data-testid="logs">
              {#if displayedLines.length === 0}
                <div class="log-line">(No logs)</div>
              {:else}
                {#each displayedLines as line, i (i)}
                  <div class="log-line">{line}</div>
                {/each}
              {/if}
            </div>
          {/if}
        </div>
      {:else}
        <p class="no-logs">Select a step to view logs</p>
      {/if}
    </div>

    <footer class="modal-footer">
      {#if liveRun.status === 'running' || liveRun.status === 'pending'}
        <button type="button" class="btn-cancel" on:click={handleCancel}>Cancel Pipeline</button>
      {/if}
      {#if liveRun.status === 'failed'}
        <!-- 12.7: offered on a FAILED run only. A debug re-run exists to
             reproduce a failure under a breakpoint. -->
        <button
          type="button"
          class="btn-secondary"
          data-testid="debug-rerun-btn"
          on:click={openDebugRerun}
        >
          Debug Re-run
        </button>
      {/if}
      <button type="button" class="btn-secondary" on:click={() => dispatch('close')}>
        Close
      </button>
    </footer>
  </div>
</div>

{#if showDebugRerun && debugPipeline}
  <DebugRerunModal
    run={liveRun}
    pipeline={debugPipeline}
    on:close={() => (showDebugRerun = false)}
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

  .logs-stale {
    padding: 0.4rem 0.6rem;
    margin-bottom: 0.5rem;
    border-radius: 4px;
    background: rgba(249, 226, 175, 0.12);
    border: 1px solid var(--warning-color);
    color: var(--warning-color);
    font-size: 0.8rem;
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
    max-height: 300px;
    overflow-y: auto;
    margin: 0;
  }

  .log-line {
    white-space: pre-wrap;
    word-break: break-all;
    /* An empty log line is still a line: without this it collapses to zero
       height and the blank rows a real transcript contains disappear. */
    min-height: 1.2em;
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
</style>
