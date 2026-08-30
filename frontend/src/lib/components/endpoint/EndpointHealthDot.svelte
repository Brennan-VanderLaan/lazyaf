<script lang="ts">
  /**
   * The derived health of one endpoint: a dot, a word, and — on hover — the
   * sentence that says what this state MEANS for a step.
   *
   * The sentence is not decoration. `degraded` and `stale` are both amber and
   * they behave completely differently (degraded RUNS down the fallback
   * protocol; stale RUNS and re-probes; `unprobed` REFUSES), so colour alone
   * would leave an operator guessing which of three outcomes they are looking
   * at. The health value itself is the BACKEND's derivation, rendered here
   * and never recomputed.
   */
  import type { EndpointHealth } from '../../api/types';
  import { healthPresentation } from '../../stores/endpoints';

  interface Props {
    health: EndpointHealth | string;
    /** Relative age of the capability record, already formatted. */
    age?: string | null;
  }

  let { health, age = null }: Props = $props();

  let presentation = $derived(healthPresentation(health));
</script>

<span
  class="health tone-{presentation.tone}"
  data-testid="endpoint-health"
  data-health={health}
  title={presentation.meaning}
>
  <span class="dot" aria-hidden="true"></span>
  <span class="label">{presentation.label}</span>
  {#if age}
    <span class="age" data-testid="endpoint-health-age">{age}</span>
  {/if}
</span>

<style>
  .health {
    display: inline-flex;
    align-items: baseline;
    gap: 0.4rem;
    font-size: 0.85rem;
    white-space: nowrap;
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    align-self: center;
    flex-shrink: 0;
    background: var(--text-muted);
  }

  .tone-good .dot { background: var(--success-color); }
  .tone-warn .dot { background: var(--warning-color); }
  .tone-bad .dot { background: var(--error-color); }
  .tone-unknown .dot {
    background: transparent;
    border: 2px dashed var(--text-muted);
    width: 10px;
    height: 10px;
  }

  .tone-good .label { color: var(--success-color); }
  .tone-warn .label { color: var(--warning-color); }
  .tone-bad .label { color: var(--error-color); }
  .tone-unknown .label { color: var(--text-muted); font-style: italic; }

  .age {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
</style>
