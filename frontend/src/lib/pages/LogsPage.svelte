<script lang="ts">
  /**
   * The Logs tab.
   *
   * TWO REGIONS AND A BUTTON, in that order of importance:
   *
   *  1. THE SYSTEM STRIP, above everything, answering "what is actually
   *     running here" before the user has selected anything. Whether the
   *     process is executing the code on disk is the question that costs the
   *     most time on this platform and the one the product could not answer
   *     at all until now.
   *
   *  2. ONE MERGED, TIMESTAMPED STREAM - not four panes. What a person
   *     debugging this needs is time correlation across sources: a backend
   *     ERROR, the step line six milliseconds later, the browser's 500. Four
   *     scrolling panes make them do that join by hand.
   *
   *  3. "Bug report…", which opens a review sheet over a bundle that has
   *     already been built, so what is reviewed is what is downloaded.
   *
   * REDACTION APPLIES TO THIS SCREEN TOO, not only to bundles. The backend
   * redacts on the way out of the ring buffer, so what is rendered here is
   * already safe - which matters because a screenshot of an unredacted token
   * is the same leak with extra steps.
   */
  import { onMount, onDestroy } from 'svelte';
  import SystemHeader from '../components/diagnostics/SystemHeader.svelte';
  import SourceRail from '../components/diagnostics/SourceRail.svelte';
  import LogStream from '../components/diagnostics/LogStream.svelte';
  import BugReportSheet from '../components/diagnostics/BugReportSheet.svelte';
  import {
    diagnosticsStore,
    systemVerdict,
    DIAGNOSTICS_UNAVAILABLE,
    type LogEntry,
  } from '../stores/diagnostics';

  let nowTick = Date.now();
  let clock: ReturnType<typeof setInterval> | null = null;

  /**
   * Which sources are shown. Membership is CLIENT-SIDE over the store's
   * buffer; see SourceRail for why it must not re-fetch.
   */
  let enabledSources = new Set<string>();
  let errorsOnly = false;
  let showSheet = false;

  /**
   * Sources we have already made a decision about.
   *
   * Kept apart from `enabledSources` so that a source the user has
   * deliberately UNTICKED is not silently re-ticked the next time it appears
   * in a poll response.
   */
  const seenSources = new Set<string>();

  $: state = $diagnosticsStore;

  /**
   * Newly-reported sources start enabled: a source that appears mid-session -
   * the first step of a run that just started - arriving unticked would look
   * exactly like a source producing no output, which is the confusion this
   * page exists to remove.
   *
   * This runs as a FUNCTION CALL rather than an inline block so the reactive
   * statement depends only on `state.sources`. Reading and writing
   * `enabledSources` in the same reactive block makes it self-invalidating.
   */
  function syncSources(sources: typeof state.sources) {
    const next = new Set(enabledSources);
    let changed = false;
    for (const source of sources) {
      if (!seenSources.has(source.id)) {
        seenSources.add(source.id);
        next.add(source.id);
        changed = true;
      }
    }
    if (changed) enabledSources = next;
  }

  $: syncSources(state.sources);

  /**
   * The rendered slice.
   *
   * A fresh array each flush, which is fine and is NOT what breaks selection:
   * LogStream's each-block is unkeyed, so identical leading entries produce
   * no DOM writes at all. What would break it is inserting into the middle of
   * the buffer or replacing it with a re-fetch - neither happens here.
   */
  $: visible = state.entries.filter((entry: LogEntry) => {
    if (!enabledSources.has(entry.source)) return false;
    if (errorsOnly) {
      const level = (entry.level || '').toUpperCase();
      return level === 'ERROR' || level === 'CRITICAL' || level === 'FATAL';
    }
    return true;
  });

  $: unavailable =
    state.systemStatus === 'unavailable' || state.logError === DIAGNOSTICS_UNAVAILABLE;

  /** Seeds the bug-report title from the newest error the stream holds. */
  $: latestError =
    [...state.entries]
      .reverse()
      .find((e) => ['ERROR', 'CRITICAL', 'FATAL'].includes((e.level || '').toUpperCase()))
      ?.message ?? '';

  function toggleSource(id: string) {
    const next = new Set(enabledSources);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    enabledSources = next;
  }

  function selectRun(id: string | null) {
    void diagnosticsStore.selectRun(id);
  }

  function toggleLive() {
    diagnosticsStore.setLive(!state.live);
  }

  onMount(() => {
    void diagnosticsStore.start();
    clock = setInterval(() => (nowTick = Date.now()), 1000);
  });

  onDestroy(() => {
    diagnosticsStore.stop();
    if (clock) clearInterval(clock);
  });
</script>

<div class="logs-page" data-testid="logs-page">
  <header class="page-header">
    <h1>Logs</h1>
    <button
      class="btn-primary"
      data-testid="open-bug-report"
      disabled={unavailable}
      title={unavailable
        ? 'This backend does not expose /api/diagnostics, so no bundle can be built here.'
        : 'Build a bundle and review it before attaching it anywhere.'}
      on:click={() => (showSheet = true)}
    >
      Bug report…
    </button>
  </header>

  <SystemHeader
    system={state.system}
    status={state.systemStatus}
    verdict={$systemVerdict}
    error={state.systemError}
    {unavailable}
    {nowTick}
  />

  <section class="stream-panel">
    <div class="stream-layout">
      <SourceRail
        sources={state.sources}
        enabled={enabledSources}
        runs={state.runs}
        selectedRunId={state.selectedRunId}
        onToggle={toggleSource}
        onSelectRun={selectRun}
      />

      <div class="stream-main">
        <div class="stream-toolbar">
          <span class="merged-label">merged · newest last</span>
          <label class="toggle">
            <input type="checkbox" bind:checked={errorsOnly} data-testid="errors-only" />
            errors only
          </label>
          <!--
            Pausing matters more than it looks: output scrolling past while
            you are reading it is the reason people give up on a log pane and
            go back to `docker logs`.
          -->
          <button class="toggle-btn" data-testid="toggle-live" on:click={toggleLive}>
            {state.live ? '⏸ pause' : '▶ live'}
          </button>
          <span class="count">{visible.length.toLocaleString()} shown</span>
        </div>

        {#if state.logStatus === 'unavailable'}
          <div class="pane-message" data-testid="logs-unavailable">
            <p>
              This backend does not serve <code>/api/diagnostics/logs</code>, so there is
              nothing to stream here. No claim is being made about whether it is healthy —
              only that it cannot be asked.
            </p>
          </div>
        {:else if state.logStatus === 'error'}
          <div class="pane-message error" data-testid="logs-error">
            <p>{state.logError}</p>
            <button class="toggle-btn" on:click={() => diagnosticsStore.refreshLogs()}>
              Retry
            </button>
          </div>
        {:else}
          <LogStream
            entries={visible}
            dropped={state.dropped}
            live={state.live}
            {errorsOnly}
            emptyMessage={state.logStatus === 'loading'
              ? 'Loading records…'
              : 'No log records for the selected sources.'}
          />
        {/if}
      </div>
    </div>
  </section>
</div>

{#if showSheet}
  <BugReportSheet
    runId={state.selectedRunId}
    initialTitle={latestError.slice(0, 120)}
    on:close={() => (showSheet = false)}
  />
{/if}

<style>
  .logs-page {
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 1rem 1.25rem;
    gap: 0.85rem;
    overflow: hidden;
  }

  .page-header {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .page-header h1 {
    margin: 0;
    font-size: 1.35rem;
    color: var(--text-color, #cdd6f4);
  }

  .btn-primary {
    margin-left: auto;
    padding: 0.45rem 0.95rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #11111b);
    border: none;
    border-radius: 5px;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }
  .btn-primary:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .stream-panel {
    flex: 1;
    min-height: 0;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    overflow: hidden;
    background: var(--surface-color, #1e1e2e);
  }

  .stream-layout {
    display: flex;
    height: 100%;
    min-height: 0;
  }

  .stream-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .stream-toolbar {
    display: flex;
    align-items: center;
    gap: 0.85rem;
    padding: 0.4rem 0.75rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    background: var(--surface-alt, #181825);
    font-size: 0.74rem;
    color: var(--text-muted, #a6adc8);
  }

  .merged-label {
    font-style: italic;
  }

  .toggle {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
  }
  .toggle input {
    margin: 0;
  }

  .toggle-btn {
    background: none;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-muted, #a6adc8);
    font-size: 0.72rem;
    padding: 0.15rem 0.5rem;
    cursor: pointer;
  }
  .toggle-btn:hover {
    color: var(--text-color, #cdd6f4);
    border-color: var(--text-muted, #6c7086);
  }

  .count {
    margin-left: auto;
    font-variant-numeric: tabular-nums;
  }

  .pane-message {
    padding: 1.25rem;
    color: var(--text-muted, #a6adc8);
    font-size: 0.83rem;
  }
  .pane-message p {
    margin: 0 0 0.6rem;
  }
  .pane-message.error p {
    color: var(--error-color, #f38ba8);
  }
  .pane-message code {
    font-family: var(--font-mono, monospace);
    font-size: 0.76rem;
    background: var(--surface-alt, #181825);
    padding: 0.05rem 0.3rem;
    border-radius: 3px;
  }
</style>
