<script lang="ts">
  /**
   * The dry-run estimate — the guardrail made visible.
   *
   * Two things this panel must never do, both of them R1:
   *  - render an unpriced variant as "$0.00" (it renders the BASIS instead), and
   *  - present a partial total as a confident one (it says "lower bound").
   */
  import type { ExperimentEstimate, EstimateBasis } from '../../api/types';
  import { formatUsd } from '../../stores/experiments';

  export let estimate: ExperimentEstimate;

  const BASIS_LABEL: Record<EstimateBasis, string> = {
    'historical-median': 'median of real past usage for these models',
    partial: 'partial — some variants have no priced history',
    'no-history': 'no priced history for any variant',
  };

  $: isLowerBound = estimate.estimate_basis !== 'historical-median';
</script>

<div class="dry-run-panel" data-testid="dry-run-panel">
  <div class="headline">
    <div class="figure">
      <span class="figure-value" data-testid="dry-run-cells">{estimate.cells}</span>
      <span class="figure-label">cells</span>
    </div>
    <div class="figure">
      <span class="figure-value" data-testid="dry-run-runs">{estimate.runs}</span>
      <span class="figure-label">runs</span>
    </div>
    <div class="figure">
      <span class="figure-value" data-testid="dry-run-estimate">
        {formatUsd(estimate.estimated_cost_usd)}
      </span>
      <span class="figure-label">{isLowerBound ? 'estimated (lower bound)' : 'estimated'}</span>
    </div>
    <div class="figure">
      <span class="figure-value" data-testid="dry-run-budget">{formatUsd(estimate.budget_usd)}</span>
      <span class="figure-label">cap</span>
    </div>
  </div>

  <p class="basis" data-testid="dry-run-basis" data-basis={estimate.estimate_basis}>
    Basis: <strong>{estimate.estimate_basis}</strong> — {BASIS_LABEL[estimate.estimate_basis]}.
    {#if isLowerBound}
      Unpriced variants contribute nothing to this total, so treat it as a floor, not a forecast.
      The cap is still enforced from observed spend at every dispatch.
    {/if}
  </p>

  {#if !estimate.within_budget}
    <div class="over-budget" data-testid="dry-run-over-budget">
      This matrix is estimated above its cap. Raise the budget or shrink the matrix —
      cells refused by the cap land as <code>skipped_budget</code>, they are not queued.
    </div>
  {/if}

  {#each estimate.warnings as warning (warning)}
    <!-- Warnings render VERBATIM: each names a real hazard the estimate cannot price. -->
    <div class="warning" data-testid="dry-run-warning">{warning}</div>
  {/each}

  {#if estimate.per_variant.length > 0}
    <table class="variants" data-testid="dry-run-variants">
      <thead>
        <tr>
          <th>Variant</th>
          <th>Agent</th>
          <th>Model</th>
          <th class="num">Runs</th>
          <th class="num">Estimate</th>
          <th class="num">Samples</th>
        </tr>
      </thead>
      <tbody>
        {#each estimate.per_variant as variant (variant.variant_index)}
          <tr data-testid="dry-run-variant-row">
            <td>{variant.label}</td>
            <td>{variant.agent}</td>
            <td>{variant.model ?? 'CLI default'}</td>
            <td class="num">{variant.runs}</td>
            <td class="num">
              {#if variant.basis === 'no-history'}
                <!--
                  The API sends "0.000000" here with basis "no-history" — the
                  variant contributed NOTHING to the total. Rendering that as
                  "$0.00" would read as "this variant is free", so the basis is
                  shown instead. Never branch on the number.
                -->
                <span class="unpriced" data-testid="dry-run-variant-unpriced">no priced history</span>
              {:else}
                {formatUsd(variant.estimate_usd)}
              {/if}
            </td>
            <td class="num">{variant.samples}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .dry-run-panel {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--primary-color, #89b4fa);
    border-radius: 8px;
    padding: 0.9rem 1rem;
  }

  .headline {
    display: flex;
    gap: 1.75rem;
    flex-wrap: wrap;
  }

  .figure {
    display: flex;
    flex-direction: column;
  }

  .figure-value {
    font-size: 1.35rem;
    font-weight: 600;
    color: var(--text-color, #cdd6f4);
    font-variant-numeric: tabular-nums;
  }

  .figure-label {
    font-size: 0.72rem;
    color: var(--text-muted, #6c7086);
  }

  .basis {
    margin: 0;
    font-size: 0.78rem;
    color: var(--text-muted, #6c7086);
  }

  .over-budget {
    font-size: 0.78rem;
    background: rgba(243, 139, 168, 0.15);
    border-left: 3px solid var(--error-color, #f38ba8);
    color: var(--error-color, #f38ba8);
    padding: 0.45rem 0.7rem;
    border-radius: 4px;
  }

  .warning {
    font-size: 0.78rem;
    background: rgba(250, 179, 135, 0.14);
    border-left: 3px solid var(--warning-color, #fab387);
    color: var(--warning-color, #fab387);
    padding: 0.45rem 0.7rem;
    border-radius: 4px;
  }

  .variants {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
  }

  .variants th,
  .variants td {
    text-align: left;
    padding: 0.3rem 0.5rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    color: var(--text-color, #cdd6f4);
  }

  .variants th {
    color: var(--text-muted, #6c7086);
    font-weight: 500;
  }

  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .unpriced {
    color: var(--warning-color, #fab387);
    font-size: 0.72rem;
  }

  code {
    font-size: 0.72rem;
    background: var(--surface-alt, #313244);
    padding: 0 0.2rem;
    border-radius: 3px;
  }
</style>
