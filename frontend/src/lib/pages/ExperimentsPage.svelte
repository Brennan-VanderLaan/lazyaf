<script lang="ts">
  /**
   * Experiments (Phase 12.6.5).
   *
   * One page, no deep modals (the Specs page idiom): the list on the left, and
   * on the right either the launch form or the selected experiment's matrix and
   * leaderboard.
   *
   * The flow is deliberately three steps — build, cost, launch — and the middle
   * one is not skippable. `MatrixBuilder` disables Launch until a dry run for
   * that exact matrix has come back, and `experimentsStore.createAndLaunch`
   * refuses an uncosted draft even if something else clicked the button.
   */
  import { onMount, onDestroy } from 'svelte';
  import { experimentsStore, cellsDone, formatUsd, formatRate } from '../stores/experiments';
  import { TERMINAL_EXPERIMENT_STATUSES } from '../api/types';
  import type { PipelineRun } from '../api/types';
  import { pipelineRuns as runsApi } from '../api/client';
  import PipelineRunViewer from '../components/PipelineRunViewer.svelte';
  import ExperimentList from '../components/experiments/ExperimentList.svelte';
  import MatrixBuilder from '../components/experiments/MatrixBuilder.svelte';
  import DryRunPanel from '../components/experiments/DryRunPanel.svelte';
  import CellGrid from '../components/experiments/CellGrid.svelte';
  import LeaderboardTable from '../components/experiments/LeaderboardTable.svelte';

  let creating = false;
  let tab: 'matrix' | 'leaderboard' = 'matrix';
  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let viewingRun: PipelineRun | null = null;
  let openRunError: string | null = null;

  onMount(() => {
    experimentsStore.loadAll();
    /**
     * A refresh tick for the selected experiment while it is still moving.
     * The live path is the `experiment_status` / `experiment_cell_status` WS
     * frames (applied by experimentsStore.applyStatusFrame / applyCellFrame);
     * this tick is what keeps the board honest when a socket drops mid-run,
     * and it stops the moment the experiment reaches a terminal status.
     */
    refreshTimer = setInterval(() => {
      const state = $experimentsStore;
      if (!state.selectedId || !state.detail || state.detailLoading) return;
      if (TERMINAL_EXPERIMENT_STATUSES.includes(state.detail.status)) return;
      void experimentsStore.loadDetail(state.selectedId);
    }, 2000);
  });

  onDestroy(() => {
    if (refreshTimer) clearInterval(refreshTimer);
  });

  $: detail = $experimentsStore.detail;
  $: isTerminal = detail ? TERMINAL_EXPERIMENT_STATUSES.includes(detail.status) : false;

  async function selectExperiment(id: string) {
    creating = false;
    tab = 'matrix';
    await experimentsStore.select(id);
  }

  function startCreate() {
    creating = true;
    experimentsStore.clearSelection();
    experimentsStore.clearEstimate();
  }

  async function showLeaderboard() {
    tab = 'leaderboard';
    if (detail) await experimentsStore.loadLeaderboard(detail.id);
  }

  /**
   * A cell IS an ordinary pipeline run, so its logs open in the ordinary run
   * viewer — no experiment-specific log plumbing exists or should.
   */
  async function openRun(pipelineRunId: string) {
    try {
      viewingRun = await runsApi.get(pipelineRunId);
    } catch (e) {
      openRunError = e instanceof Error ? e.message : 'Failed to open run';
    }
  }

  async function handleLaunched(event: CustomEvent<{ id: string }>) {
    creating = false;
    tab = 'matrix';
    await experimentsStore.select(event.detail.id);
  }
</script>

<div class="experiments-page" data-testid="experiments-page">
  <header class="page-header">
    <div class="header-left">
      <h1>Experiments</h1>
      <span class="subtitle">Matrix fan-out over models and prompts — reported, not ranked</span>
    </div>
    <button class="btn-primary" data-testid="experiment-create-btn" on:click={startCreate}>
      + New Experiment
    </button>
  </header>

  {#if $experimentsStore.error}
    <div class="error-banner" data-testid="experiment-error">{$experimentsStore.error}</div>
  {/if}
  {#if openRunError}
    <div class="error-banner" data-testid="open-run-error">{openRunError}</div>
  {/if}

  <div class="columns">
    <aside class="sidebar">
      {#if $experimentsStore.loading && $experimentsStore.list.length === 0}
        <p class="loading-hint">Loading experiments...</p>
      {:else if $experimentsStore.list.length === 0}
        <div class="empty-state" data-testid="experiments-empty">
          <span class="empty-icon">🔬</span>
          <p>No experiments yet. Build a matrix, cost it, then launch it.</p>
        </div>
      {:else}
        <ExperimentList
          items={$experimentsStore.list}
          selectedId={$experimentsStore.selectedId}
          onSelect={selectExperiment}
        />
      {/if}
    </aside>

    <section class="detail">
      {#if creating}
        <h2>New experiment</h2>
        <MatrixBuilder on:launched={handleLaunched} />
        {#if $experimentsStore.estimate}
          <DryRunPanel estimate={$experimentsStore.estimate} />
        {/if}
      {:else if detail}
        <div class="detail-header">
          <div>
            <h2>{detail.name}</h2>
            <p class="detail-sub">
              {detail.target_type.replace('_', ' ')} · {cellsDone(detail)}/{detail.cells_total} cells ·
              spend {formatUsd(detail.spend_usd)} of {formatUsd(detail.budget_usd)}
              {#if detail.cost_coverage !== null && detail.cost_coverage < 1}
                · cost coverage {formatRate(detail.cost_coverage)} (the cap is only enforced
                on the priced share)
              {/if}
            </p>
          </div>
          <div class="detail-actions">
            {#if !isTerminal}
              <button class="btn-secondary" data-testid="abort-experiment-btn" on:click={() => experimentsStore.abort(detail.id)}>
                Abort
              </button>
            {/if}
            {#if detail.status === 'draft'}
              <button class="btn-secondary" data-testid="estimate-draft-btn" on:click={() => experimentsStore.estimateSaved(detail.id)}>
                Estimate
              </button>
              <button
                class="btn-primary"
                data-testid="launch-draft-btn"
                disabled={$experimentsStore.estimateKey !== `saved:${detail.id}` || $experimentsStore.launching}
                on:click={() => experimentsStore.launchSaved(detail.id)}
              >
                Launch
              </button>
            {/if}
          </div>
        </div>

        {#if detail.stalled}
          <!--
            The pump lives in the backend process. A restart strands pending
            cells; that is reported here rather than looking like a slow run.
          -->
          <div class="warning-banner" data-testid="experiment-stalled-banner">
            This experiment is stalled: it is marked running, no cell is live, and cells are
            still pending — the dispatch pump did not survive a backend restart.
            <button class="btn-small" data-testid="resume-experiment-btn" on:click={() => experimentsStore.resume(detail.id)}>
              Resume
            </button>
          </div>
        {/if}

        {#if detail.status === 'budget_exhausted'}
          <div class="warning-banner" data-testid="experiment-budget-banner">
            The cap stopped dispatch with cells still pending; those cells are
            <code>skipped_budget</code>, not queued.
            {#if Number(detail.budget_overrun_usd) > 0}
              Cells already in flight when the cap tripped spent
              {formatUsd(detail.budget_overrun_usd)} over it.
            {/if}
          </div>
        {/if}

        {#if $experimentsStore.estimate && detail.status === 'draft'}
          <DryRunPanel estimate={$experimentsStore.estimate} />
        {/if}

        <nav class="tabs">
          <button
            class="tab"
            class:active={tab === 'matrix'}
            data-testid="tab-matrix"
            on:click={() => (tab = 'matrix')}
          >
            Matrix
          </button>
          <button
            class="tab"
            class:active={tab === 'leaderboard'}
            data-testid="tab-leaderboard"
            on:click={showLeaderboard}
          >
            Leaderboard
          </button>
        </nav>

        {#if tab === 'matrix'}
          <CellGrid cells={detail.cells} onOpenRun={openRun} />
        {:else if $experimentsStore.leaderboardLoading && !$experimentsStore.leaderboard}
          <p class="loading-hint">Loading leaderboard...</p>
        {:else if $experimentsStore.leaderboard}
          <LeaderboardTable leaderboard={$experimentsStore.leaderboard} />
        {/if}
      {:else}
        <div class="empty-state">
          <p>Select an experiment, or build a new one.</p>
        </div>
      {/if}
    </section>
  </div>
</div>

{#if viewingRun}
  <PipelineRunViewer run={viewingRun} on:close={() => (viewingRun = null)} />
{/if}

<style>
  .experiments-page {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 1.5rem 2rem;
    overflow: hidden;
  }

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    flex-wrap: wrap;
    gap: 0.75rem;
  }

  .header-left {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
  }

  .page-header h1 {
    margin: 0;
    font-size: 1.5rem;
    color: var(--text-color, #cdd6f4);
  }

  .subtitle {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .columns {
    flex: 1;
    display: grid;
    grid-template-columns: minmax(240px, 22rem) 1fr;
    gap: 1.25rem;
    min-height: 0;
  }

  .sidebar,
  .detail {
    overflow-y: auto;
    min-height: 0;
    padding-bottom: 1.5rem;
  }

  .detail {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
  }

  .detail h2 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--text-color, #cdd6f4);
  }

  .detail-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    flex-wrap: wrap;
  }

  .detail-sub {
    margin: 0.2rem 0 0;
    font-size: 0.78rem;
    color: var(--text-muted, #6c7086);
  }

  .detail-actions {
    display: flex;
    gap: 0.5rem;
  }

  .tabs {
    display: flex;
    gap: 0.4rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .tab {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted, #6c7086);
    font-family: inherit;
    font-size: 0.85rem;
    padding: 0.4rem 0.7rem;
    cursor: pointer;
  }

  .tab.active {
    color: var(--text-color, #cdd6f4);
    border-bottom-color: var(--primary-color, #89b4fa);
  }

  .error-banner {
    background: rgba(243, 139, 168, 0.15);
    border: 1px solid var(--error-color, #f38ba8);
    color: var(--error-color, #f38ba8);
    border-radius: 6px;
    padding: 0.6rem 0.9rem;
    margin-bottom: 0.75rem;
    font-size: 0.85rem;
  }

  .warning-banner {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    background: rgba(250, 179, 135, 0.15);
    border: 1px solid var(--warning-color, #fab387);
    color: var(--warning-color, #fab387);
    border-radius: 6px;
    padding: 0.6rem 0.9rem;
    font-size: 0.8rem;
  }

  .loading-hint {
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 2.5rem 1rem;
    color: var(--text-muted, #6c7086);
    text-align: center;
  }

  .empty-icon {
    font-size: 2.5rem;
  }

  code {
    font-size: 0.72rem;
    background: var(--surface-alt, #313244);
    padding: 0 0.2rem;
    border-radius: 3px;
  }

  .btn-primary {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    border: none;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
  }

  .btn-secondary {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    border: none;
    background: var(--surface-alt, #313244);
    color: var(--text-color, #cdd6f4);
    font-size: 0.9rem;
    cursor: pointer;
  }

  .btn-small {
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    border: 1px solid currentColor;
    background: transparent;
    color: inherit;
    font-family: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
