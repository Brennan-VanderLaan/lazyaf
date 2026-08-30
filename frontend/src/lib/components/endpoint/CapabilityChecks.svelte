<script lang="ts">
  /**
   * The capability row: `tools`, `stream`, `usage` and the context window.
   *
   * THE ONE RULE THIS COMPONENT EXISTS TO ENFORCE: each capability has THREE
   * states, and "never probed" is rendered as visibly different from "probed
   * and not supported".
   *
   *   supported    ✓  green
   *   unsupported  ✗  amber, with the consequence spelled out
   *   never probed  ?  dashed grey outline, italic
   *
   * A checkbox has two states, so a checkbox here would collapse "we asked
   * and it cannot tool-call" into "we never asked" — and those lead to
   * opposite outcomes: `false` routes the fallback protocol and the step
   * RUNS, `null` makes dispatch REFUSE. That silent downgrade is exactly what
   * standing rule R1 forbids, and it is why `supports_tools` is nullable in
   * the database at all.
   */
  import type { ModelEndpoint } from '../../api/types';
  import { capabilityCells, contextWindowLabel } from '../../stores/endpoints';

  interface Props {
    endpoint: ModelEndpoint;
  }

  let { endpoint }: Props = $props();

  let cells = $derived(capabilityCells(endpoint));
  let contextLabel = $derived(contextWindowLabel(endpoint));
  let contextUnknown = $derived(endpoint.capabilities.context_window === null);

  const GLYPH: Record<string, string> = {
    supported: '✓',
    unsupported: '✗',
    unprobed: '?',
  };
</script>

<div class="caps" data-testid="endpoint-capabilities">
  {#each cells as cell (cell.key)}
    <span
      class="cap state-{cell.state}"
      data-testid="endpoint-cap-{cell.key}"
      data-state={cell.state}
      title={cell.detail}
    >
      <span class="glyph" aria-hidden="true">{GLYPH[cell.state]}</span>
      <span class="name">{cell.label}</span>
    </span>
  {/each}
  <span
    class="cap ctx"
    class:state-unprobed={contextUnknown}
    data-testid="endpoint-cap-context"
    data-context-known={contextUnknown ? 'false' : 'true'}
    title={contextUnknown
      ? 'Neither the operator nor the probe supplied a context window. The harness assumes 8192 tokens and says so, loudly, in the step log.'
      : `Effective context window: ${endpoint.capabilities.context_window} tokens (${endpoint.context_window_source ?? 'probe'})`}
  >
    {contextLabel}
  </span>
</div>

<style>
  .caps {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    align-items: center;
  }

  .cap {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
    font-size: 0.75rem;
    background: var(--badge-bg);
    border: 1px solid transparent;
    white-space: nowrap;
  }

  .glyph {
    font-weight: 700;
  }

  .state-supported {
    color: var(--success-color);
  }

  .state-unsupported {
    color: var(--warning-color);
  }

  /* Dashed and italic, so "never asked" cannot be misread at a glance as a
     plain unchecked box. */
  .state-unprobed {
    color: var(--text-muted);
    background: transparent;
    border: 1px dashed var(--border-color);
    font-style: italic;
  }

  .ctx {
    color: var(--text-color);
    font-variant-numeric: tabular-nums;
  }
</style>
