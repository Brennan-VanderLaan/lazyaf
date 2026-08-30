<script lang="ts">
  import type { ExperimentSummary } from '../../api/types';
  import { cellsDone, formatUsd, formatRate } from '../../stores/experiments';

  export let items: ExperimentSummary[] = [];
  export let selectedId: string | null = null;
  export let onSelect: (id: string) => void = () => {};

  /**
   * Spend as a fraction of the cap, for the bar WIDTH only: clamped, and never
   * displayed. Every dollar amount the user actually reads goes through
   * `formatUsd` on the original decimal string, so no displayed figure is ever
   * rounded through a float.
   */
  function spendFraction(spend: string, budget: string): number {
    const s = Number(spend);
    const b = Number(budget);
    if (!Number.isFinite(s) || !Number.isFinite(b) || b <= 0) return 0;
    return Math.min(1, Math.max(0, s / b));
  }
</script>

<div class="experiment-list" data-testid="experiment-list">
  {#each items as exp (exp.id)}
    <button
      type="button"
      class="experiment-item"
      class:selected={exp.id === selectedId}
      data-testid="experiment-item"
      data-experiment-id={exp.id}
      data-status={exp.status}
      on:click={() => onSelect(exp.id)}
    >
      <div class="row-top">
        <span class="name">{exp.name}</span>
        <span class="status status-{exp.status}" data-testid="experiment-status">
          {exp.status.replace('_', ' ')}
        </span>
      </div>

      <div class="row-meta">
        <span class="target">{exp.target_type.replace('_', ' ')}</span>
        <span class="cells" data-testid="experiment-cell-progress">
          {cellsDone(exp)}/{exp.cells_total} cells
        </span>
        {#if exp.stalled}
          <!-- The in-process pump died with the backend. Never hidden. -->
          <span class="badge badge-warn" data-testid="experiment-stalled">stalled</span>
        {/if}
      </div>

      <div class="spend">
        <div class="bar" aria-hidden="true">
          <div
            class="fill"
            class:over={spendFraction(exp.spend_usd, exp.budget_usd) >= 1}
            style="width: {spendFraction(exp.spend_usd, exp.budget_usd) * 100}%"
          ></div>
        </div>
        <span class="spend-text" data-testid="experiment-spend">
          {formatUsd(exp.spend_usd)} / {formatUsd(exp.budget_usd)}
        </span>
        {#if exp.cost_coverage !== null && exp.cost_coverage < 1}
          <!--
            Coverage below 1.0 means some usage rows reported no cost, and
            unknown-cost rows count as ZERO against the cap. The budget is
            partially unenforced and the row has to say so.
          -->
          <span class="badge badge-warn" data-testid="experiment-cost-coverage">
            cost coverage {formatRate(exp.cost_coverage)}
          </span>
        {/if}
        <!-- Overrun is a comparison against zero only; the figure itself is
             rendered from the untouched decimal string. -->
        {#if Number(exp.budget_overrun_usd) > 0}
          <span class="badge badge-error" data-testid="experiment-overrun">
            over cap by {formatUsd(exp.budget_overrun_usd)}
          </span>
        {/if}
      </div>
    </button>
  {/each}
</div>

<style>
  .experiment-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .experiment-item {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    width: 100%;
    text-align: left;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    padding: 0.7rem 0.9rem;
    color: var(--text-color, #cdd6f4);
    font-family: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .experiment-item.selected {
    border-color: var(--primary-color, #89b4fa);
  }

  .row-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;
  }

  .name {
    font-weight: 600;
    font-size: 0.95rem;
  }

  .status {
    text-transform: uppercase;
    font-size: 0.68rem;
    letter-spacing: 0.04em;
    padding: 0.15rem 0.45rem;
    border-radius: 4px;
    background: var(--surface-alt, #313244);
    color: var(--text-muted, #6c7086);
    white-space: nowrap;
  }

  .status-running {
    background: rgba(137, 180, 250, 0.2);
    color: var(--primary-color, #89b4fa);
  }

  .status-complete {
    background: rgba(166, 227, 161, 0.2);
    color: var(--success-color, #a6e3a1);
  }

  .status-aborted,
  .status-budget_exhausted {
    background: rgba(250, 179, 135, 0.2);
    color: var(--warning-color, #fab387);
  }

  .row-meta {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    color: var(--text-muted, #6c7086);
    font-size: 0.78rem;
  }

  .spend {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .bar {
    flex: 1;
    min-width: 80px;
    height: 5px;
    border-radius: 3px;
    background: var(--surface-alt, #313244);
    overflow: hidden;
  }

  .fill {
    height: 100%;
    background: var(--primary-color, #89b4fa);
  }

  .fill.over {
    background: var(--error-color, #f38ba8);
  }

  .spend-text {
    font-variant-numeric: tabular-nums;
    font-size: 0.78rem;
    color: var(--text-muted, #6c7086);
  }

  .badge {
    font-size: 0.68rem;
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
  }

  .badge-warn {
    background: rgba(250, 179, 135, 0.18);
    color: var(--warning-color, #fab387);
  }

  .badge-error {
    background: rgba(243, 139, 168, 0.18);
    color: var(--error-color, #f38ba8);
  }
</style>
