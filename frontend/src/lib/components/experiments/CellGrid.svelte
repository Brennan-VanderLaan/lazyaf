<script lang="ts">
  /**
   * The matrix: one row per variant, one chip per repeat.
   *
   * `failed` and `error` get DIFFERENT colours and different words, because
   * they are different facts: a red suite is a measurement that counts, a
   * crashed cell measured nothing and is excluded from every denominator.
   */
  import type { ExperimentCell, ExperimentCellStatus } from '../../api/types';

  export let cells: ExperimentCell[] = [];
  export let onOpenRun: (pipelineRunId: string) => void = () => {};

  interface VariantRow {
    variantIndex: number;
    label: string;
    agent: string;
    model: string | null;
    promptVersion: number | null;
    cells: ExperimentCell[];
  }

  const STATUS_TITLE: Record<ExperimentCellStatus, string> = {
    pending: 'queued, not dispatched',
    dispatching: 'claimed, run not created yet',
    running: 'running',
    passed: 'ran and satisfied the suite',
    failed: 'ran and the suite was red — a measurement, counted',
    error: 'ran and measured nothing — excluded from pass rates',
    cancelled: 'cancelled before dispatch',
    skipped_budget: 'refused by the budget cap',
  };

  function groupByVariant(rows: ExperimentCell[]): VariantRow[] {
    const byVariant = new Map<number, VariantRow>();
    for (const cell of [...rows].sort((a, b) => a.cell_index - b.cell_index)) {
      let row = byVariant.get(cell.variant_index);
      if (!row) {
        row = {
          variantIndex: cell.variant_index,
          label: cell.label ?? `variant ${cell.variant_index}`,
          agent: cell.agent,
          model: cell.model,
          promptVersion: cell.prompt_version,
          cells: [],
        };
        byVariant.set(cell.variant_index, row);
      }
      row.cells.push(cell);
    }
    return [...byVariant.values()].sort((a, b) => a.variantIndex - b.variantIndex);
  }

  $: variants = groupByVariant(cells);
</script>

<div class="cell-grid" data-testid="cell-grid">
  {#each variants as variant (variant.variantIndex)}
    <div class="variant-row" data-testid="cell-grid-row" data-variant-index={variant.variantIndex}>
      <div class="coords">
        <span class="label">{variant.label}</span>
        <span class="sub">
          {variant.agent}
          · {variant.model ?? 'CLI default'}
          {#if variant.promptVersion !== null}· prompt v{variant.promptVersion}{/if}
        </span>
      </div>
      <div class="chips">
        {#each variant.cells as cell (cell.id)}
          <button
            type="button"
            class="cell-chip status-{cell.status}"
            data-testid="cell-chip"
            data-cell-index={cell.cell_index}
            data-status={cell.status}
            title="cell {cell.cell_index}: {STATUS_TITLE[cell.status]}{cell.error ? ` — ${cell.error}` : ''}"
            disabled={!cell.pipeline_run_id}
            on:click={() => cell.pipeline_run_id && onOpenRun(cell.pipeline_run_id)}
          >
            {cell.repeat_index + 1}
          </button>
        {/each}
      </div>
    </div>
  {/each}

  {#if variants.length === 0}
    <p class="empty">No cells yet — the matrix is created at launch.</p>
  {/if}
</div>

<div class="legend" data-testid="cell-grid-legend">
  <span class="swatch status-passed"></span> passed
  <span class="swatch status-failed"></span> failed (suite red — counted)
  <span class="swatch status-error"></span> error (nothing measured — excluded)
  <span class="swatch status-skipped_budget"></span> skipped by cap
</div>

<style>
  .cell-grid {
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
  }

  .variant-row {
    display: grid;
    grid-template-columns: minmax(10rem, 18rem) 1fr;
    gap: 0.75rem;
    align-items: center;
  }

  .coords {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .label {
    font-size: 0.82rem;
    color: var(--text-color, #cdd6f4);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sub {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chips {
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
  }

  .cell-chip {
    width: 1.7rem;
    height: 1.7rem;
    border-radius: 4px;
    border: 1px solid var(--border-color, #45475a);
    background: var(--surface-alt, #313244);
    color: var(--text-muted, #6c7086);
    font-size: 0.72rem;
    font-family: inherit;
    cursor: pointer;
  }

  .cell-chip:disabled {
    cursor: default;
  }

  .swatch {
    display: inline-block;
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 3px;
    margin-left: 0.6rem;
    vertical-align: middle;
  }

  .status-running {
    background: rgba(137, 180, 250, 0.35);
    color: var(--primary-color, #89b4fa);
  }

  .status-dispatching {
    background: rgba(137, 180, 250, 0.18);
  }

  .status-passed {
    background: rgba(166, 227, 161, 0.35);
    color: var(--success-color, #a6e3a1);
  }

  .status-failed {
    background: rgba(243, 139, 168, 0.35);
    color: var(--error-color, #f38ba8);
  }

  /* error is deliberately NOT the failure colour: it is a different fact. */
  .status-error {
    background: rgba(203, 166, 247, 0.35);
    color: #cba6f7;
  }

  .status-cancelled,
  .status-skipped_budget {
    background: rgba(250, 179, 135, 0.28);
    color: var(--warning-color, #fab387);
  }

  .legend {
    margin-top: 0.6rem;
    font-size: 0.72rem;
    color: var(--text-muted, #6c7086);
  }

  .empty {
    color: var(--text-muted, #6c7086);
    font-size: 0.82rem;
    font-style: italic;
  }
</style>
