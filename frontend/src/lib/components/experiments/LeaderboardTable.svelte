<script lang="ts">
  /**
   * The leaderboard. 12.6.5 REPORTS; it does not rank.
   *
   * The `ranked: false` note renders verbatim and always, above the table, and
   * the sort control is labelled a client-side convenience — because sorting a
   * column is not a claim that the top row won. Ranking needs the paired
   * cluster bootstrap and the separability rule, which arrive with 13.4.
   *
   * The headline rate is the MACRO average over criteria (equal weight per
   * criterion); the pooled micro rate is carried as a footnote so a single
   * criterion with forty tests cannot own the number.
   */
  import type { Leaderboard, LeaderboardVariant } from '../../api/types';
  import { CostBasisPill } from '../endpoint';
  import { NOT_RANKED_NOTE } from '../../api/types';
  import { formatUsd, formatRate, formatDuration } from '../../stores/experiments';

  export let leaderboard: Leaderboard;

  type SortKey = 'variant' | 'pass_rate' | 'cost';
  let sortKey: SortKey = 'variant';
  let expanded: string[] = [];

  function toggle(variantIndex: number) {
    const key = String(variantIndex);
    expanded = expanded.includes(key) ? expanded.filter(k => k !== key) : [...expanded, key];
  }

  /**
   * Client-side ordering only. Nulls always sink to the bottom: an unmeasured
   * variant must never be sorted as though it scored zero.
   */
  function sorted(variants: LeaderboardVariant[], key: SortKey): LeaderboardVariant[] {
    const rows = [...variants];
    if (key === 'variant') return rows.sort((a, b) => a.variant_index - b.variant_index);
    if (key === 'pass_rate') {
      return rows.sort((a, b) => {
        if (a.pass_rate === null && b.pass_rate === null) return 0;
        if (a.pass_rate === null) return 1;
        if (b.pass_rate === null) return -1;
        return b.pass_rate - a.pass_rate;
      });
    }
    return rows.sort((a, b) => Number(a.cost_usd_total) - Number(b.cost_usd_total));
  }

  $: rows = sorted(leaderboard.variants, sortKey);
</script>

<div class="leaderboard" data-testid="leaderboard">
  <!--
    Rendered verbatim from the API. NOT_RANKED_NOTE is the same literal the
    backend sends; it is single-sourced in api/types.ts so the component, the
    backend test and the Playwright spec all assert one string.
  -->
  <p class="not-ranked" data-testid="leaderboard-not-ranked-note">
    {leaderboard.note || NOT_RANKED_NOTE}
  </p>

  {#each leaderboard.warnings as warning (warning)}
    <div class="warning" data-testid="leaderboard-warning">{warning}</div>
  {/each}

  <div class="sort-row">
    <label>
      <span>Sort (a client-side convenience — not a ranking)</span>
      <select data-testid="leaderboard-sort-select" bind:value={sortKey}>
        <option value="variant">matrix order</option>
        <option value="pass_rate">pass rate</option>
        <option value="cost">cost</option>
      </select>
    </label>
  </div>

  <table class="leaderboard-table" data-testid="leaderboard-table">
    <thead>
      <tr>
        <th>Variant</th>
        <th>Agent / model</th>
        <th class="num">Measured</th>
        <th class="num">Pass rate (macro)</th>
        <th class="num">Cost</th>
        <th class="num">Median / run</th>
        <th class="num">Median wall clock</th>
        <th class="num">Errors</th>
      </tr>
    </thead>
    <tbody>
      {#each rows as variant (variant.variant_index)}
        <tr
          class="variant-row"
          data-testid="leaderboard-row"
          data-variant-index={variant.variant_index}
        >
          <td>
            <button
              type="button"
              class="expand"
              data-testid="leaderboard-expand-btn"
              on:click={() => toggle(variant.variant_index)}
            >
              {expanded.includes(String(variant.variant_index)) ? '▾' : '▸'}
              {variant.label}
            </button>
            {#if variant.insufficient_repeats}
              <span class="badge badge-warn" data-testid="leaderboard-insufficient-repeats">
                n &lt; 3 — point values only
              </span>
            {/if}
            {#each variant.warnings as warning (warning)}
              <span class="badge badge-warn" data-testid="leaderboard-row-warning">{warning}</span>
            {/each}
          </td>
          <td class="mono">
            {variant.agent} / {variant.model ?? 'CLI default'}
            {#if variant.prompt_version !== null}<span class="sub">prompt v{variant.prompt_version}</span>{/if}
          </td>
          <td class="num">
            {variant.cells_measured}/{variant.cells_total}
            {#if variant.cells_skipped_budget > 0}
              <span class="sub warn">{variant.cells_skipped_budget} skipped by cap</span>
            {/if}
          </td>
          <td class="num" data-testid="leaderboard-pass-rate">
            {formatRate(variant.pass_rate)}
            {#if variant.pass_rate === null && variant.reason}
              <!-- Zero denominator: the reason, never a 0%. -->
              <span class="sub" data-testid="leaderboard-pass-rate-reason">{variant.reason}</span>
            {:else if variant.pass_rate_micro !== null}
              <span class="sub">micro {formatRate(variant.pass_rate_micro)}</span>
            {/if}
          </td>
          <td class="num">
            <!--
              M14 decision 4, UI half. A variant with NO cost data renders as
              an ABSENCE - struck through, "no cost data" - never as a number.
              The failure this prevents is a board that puts an Anthropic
              invoice next to a locally-derived node-rate figure and lets
              someone quote the ratio; a $0.00 in this column would read as
              "this was free" when the truth is "we do not know".
            -->
            {#if variant.cost_coverage === 0}
              <span class="no-cost" data-testid="leaderboard-no-cost">
                <s>{formatUsd(variant.cost_usd_total)}</s>
                <span class="sub warn">no cost data</span>
              </span>
            {:else}
              {formatUsd(variant.cost_usd_total)}
              {#if variant.cost_coverage !== null && variant.cost_coverage < 1}
                <span class="sub warn" data-testid="leaderboard-cost-coverage">
                  coverage {formatRate(variant.cost_coverage)}
                </span>
              {/if}
            {/if}
            <span class="sub"><CostBasisPill coverage={variant.cost_coverage} /></span>
          </td>
          <td class="num">{formatUsd(variant.cost_usd_per_run_median)}</td>
          <td class="num">{formatDuration(variant.wall_clock_ms_median)}</td>
          <td class="num">
            {variant.cells_errored}/{variant.cells_total}
            <span class="sub">{formatRate(variant.error_rate)}</span>
          </td>
        </tr>

        {#if expanded.includes(String(variant.variant_index))}
          <tr class="criteria-row">
            <td colspan="8">
              <table class="criteria">
                <tbody>
                  {#each variant.criteria as criterion (criterion.criterion_id ?? 'unlinked')}
                    <tr data-testid="criterion-rate">
                      <td class="criterion-text">
                        {criterion.criterion_text ?? criterion.criterion_id ?? 'unlinked'}
                      </td>
                      <td class="num">
                        {formatRate(criterion.pass_rate)}
                        {#if criterion.pass_rate === null && criterion.reason}
                          <span class="sub">{criterion.reason}</span>
                        {/if}
                      </td>
                      <td class="num sub">
                        {criterion.passed} passed · {criterion.failed} failed ·
                        {criterion.skipped} skipped (excluded)
                      </td>
                    </tr>
                  {/each}
                  {#if variant.unlinked_tests}
                    <tr data-testid="criterion-rate">
                      <td class="criterion-text">
                        tests linked to no criterion
                        <span class="sub">counted separately, never dropped</span>
                      </td>
                      <td class="num">{formatRate(variant.unlinked_tests.pass_rate)}</td>
                      <td class="num sub">
                        {variant.unlinked_tests.passed} passed ·
                        {variant.unlinked_tests.failed} failed ·
                        {variant.unlinked_tests.skipped} skipped
                      </td>
                    </tr>
                  {/if}
                  {#if variant.criteria.length === 0 && !variant.unlinked_tests}
                    <tr>
                      <td colspan="3" class="sub">
                        No test evidence for this variant — add a verify step to measure one.
                      </td>
                    </tr>
                  {/if}
                </tbody>
              </table>
            </td>
          </tr>
        {/if}
      {/each}
    </tbody>
  </table>

  {#if leaderboard.variants.length === 0}
    <p class="empty">No variants have reported yet.</p>
  {/if}
</div>

<style>
  .leaderboard {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .not-ranked {
    margin: 0;
    font-size: 0.78rem;
    line-height: 1.4;
    color: var(--text-muted, #6c7086);
    background: var(--surface-alt, #313244);
    border-left: 3px solid var(--primary-color, #89b4fa);
    padding: 0.55rem 0.75rem;
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

  .sort-row {
    font-size: 0.72rem;
    color: var(--text-muted, #6c7086);
  }

  .sort-row select {
    margin-left: 0.4rem;
    padding: 0.2rem 0.35rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-family: inherit;
    font-size: 0.72rem;
  }

  .leaderboard-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8rem;
  }

  .leaderboard-table th,
  .leaderboard-table td {
    text-align: left;
    padding: 0.4rem 0.5rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    color: var(--text-color, #cdd6f4);
    vertical-align: top;
  }

  .leaderboard-table th {
    color: var(--text-muted, #6c7086);
    font-weight: 500;
    white-space: nowrap;
  }

  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .mono {
    font-size: 0.75rem;
  }

  .sub {
    display: block;
    font-size: 0.68rem;
    color: var(--text-muted, #6c7086);
    font-weight: 400;
  }

  .sub.warn {
    color: var(--warning-color, #fab387);
  }

  .expand {
    background: transparent;
    border: none;
    color: var(--text-color, #cdd6f4);
    font-family: inherit;
    font-size: 0.8rem;
    cursor: pointer;
    padding: 0;
  }

  .badge {
    display: inline-block;
    font-size: 0.66rem;
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
    margin-top: 0.2rem;
  }

  .badge-warn {
    background: rgba(250, 179, 135, 0.18);
    color: var(--warning-color, #fab387);
  }

  .criteria {
    width: 100%;
    border-collapse: collapse;
  }

  .criteria td {
    padding: 0.25rem 0.4rem;
    border-bottom: 1px dashed var(--border-color, #45475a);
    font-size: 0.75rem;
  }

  .criterion-text {
    color: var(--text-color, #cdd6f4);
  }

  .empty {
    color: var(--text-muted, #6c7086);
    font-style: italic;
    font-size: 0.82rem;
  }

  /* M14: an unpriced variant's cost is an absence, rendered as one. */
  .no-cost s {
    color: var(--text-muted);
  }
</style>
