<script lang="ts">
  /**
   * How a cost number was arrived at — decision 4's UI half.
   *
   * The failure this exists to prevent is a board that compares an Anthropic
   * INVOICE against a locally-derived node-rate ESTIMATE and lets someone
   * quote the ratio in public. `cost_source` is a first-class column
   * precisely so the two can be told apart, and the board has to render the
   * distinction or the column is decorative.
   *
   *   billed       every row `cli-reported` — the provider billed us this
   *   node-priced  every row `gpu-node`     — rate × container_seconds × gpu_fraction
   *   mixed        both, in one aggregate   — not comparable without a note
   *   unpriced     no row carries a cost    — rendered as an ABSENCE, never as $0.00
   *
   * DEVIATION, stated rather than hidden: the four-state form needs the
   * `by_source` counts, which `GET /api/experiments/{id}/leaderboard` does
   * not currently return on `LeaderboardVariant` (only `cost_coverage` does).
   * When `bySource` is absent this component falls back to what coverage can
   * honestly support — `unpriced` (coverage 0), `partial` (0 < c < 1),
   * `priced` (c = 1) — and never invents the billed/node-priced split it
   * cannot see. Once the backend adds `by_source` the four-state branch below
   * lights up with no caller changes.
   */
  interface Props {
    /** `cost_source` -> row count, when the API supplies it. */
    bySource?: Record<string, number> | null;
    /** Fraction of rows carrying a non-null cost. */
    coverage?: number | null;
  }

  let { bySource = null, coverage = null }: Props = $props();

  interface Basis {
    key: string;
    label: string;
    tone: 'good' | 'warn' | 'bad' | 'muted';
    title: string;
  }

  function fromSources(counts: Record<string, number>): Basis {
    const billed = counts['cli-reported'] ?? 0;
    const node = counts['gpu-node'] ?? 0;
    const unknown = counts['unknown'] ?? 0;
    if (billed === 0 && node === 0) {
      return {
        key: 'unpriced',
        label: 'unpriced',
        tone: 'bad',
        title: `No row in this aggregate carries a cost (${unknown} unknown). There is no cost number here — an absence, not a zero.`,
      };
    }
    if (billed > 0 && node > 0) {
      return {
        key: 'mixed',
        label: 'mixed',
        tone: 'warn',
        title: `${billed} provider-billed row(s) and ${node} node-priced row(s) in one aggregate. A billed dollar and a rate × seconds dollar are not the same measurement; see METHOD before comparing them.`,
      };
    }
    if (unknown > 0) {
      return {
        key: billed > 0 ? 'billed-partial' : 'node-priced-partial',
        label: billed > 0 ? 'billed (partial)' : 'node-priced (partial)',
        tone: 'warn',
        title: `${unknown} row(s) carry no cost at all, so this total is a lower bound.`,
      };
    }
    return billed > 0
      ? {
          key: 'billed',
          label: 'billed',
          tone: 'good',
          title: 'Every row reports a cost the provider billed us.',
        }
      : {
          key: 'node-priced',
          label: 'node-priced',
          tone: 'good',
          title: 'Every row is priced from a node rate × container seconds × gpu_fraction. A derived figure, not an invoice.',
        };
  }

  function fromCoverage(value: number | null): Basis {
    if (value === null) {
      return {
        key: 'unknown',
        label: 'no basis',
        tone: 'muted',
        title: 'The API did not report cost coverage for this row.',
      };
    }
    if (value <= 0) {
      return {
        key: 'unpriced',
        label: 'unpriced',
        tone: 'bad',
        title: 'No row in this aggregate carries a cost. There is no cost number here — an absence, not a zero.',
      };
    }
    if (value < 1) {
      return {
        key: 'partial',
        label: 'partial',
        tone: 'warn',
        title: `Only ${Math.round(value * 100)}% of rows carry a cost, so this total is a lower bound.`,
      };
    }
    return {
      key: 'priced',
      label: 'priced',
      tone: 'good',
      title: 'Every row carries a cost.',
    };
  }

  let basis = $derived(bySource ? fromSources(bySource) : fromCoverage(coverage));
</script>

<span
  class="pill tone-{basis.tone}"
  data-testid="cost-basis-pill"
  data-basis={basis.key}
  title={basis.title}
>{basis.label}</span>

<style>
  .pill {
    display: inline-block;
    padding: 0.05rem 0.4rem;
    border-radius: 999px;
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    border: 1px solid currentColor;
    white-space: nowrap;
  }

  .tone-good { color: var(--success-color); }
  .tone-warn { color: var(--warning-color); }
  .tone-bad { color: var(--error-color); }
  .tone-muted { color: var(--text-muted); }
</style>
