<script lang="ts">
  /**
   * THE ONE capability display. Endpoints page, endpoint modal and Playground
   * all render this component — there is no second copy, because a second
   * copy is a second place for "never probed" to quietly start looking like
   * "not supported".
   *
   * THE RULE THIS COMPONENT EXISTS TO ENFORCE: a capability is not a
   * checkbox. `tools` / `stream` / `usage` have THREE states and each modality
   * has SIX, and no two of them may render alike:
   *
   *   supported        ✓  green,  solid
   *   not supported    ✗  amber,  solid
   *   not probed       ?  grey,   DASHED + italic      <- the common case
   *   probe failed     !  red,    DASHED
   *   undetectable     ~  grey,   DOTTED + italic
   *   not expressible  ⊘  inert,  solid, struck label
   *
   * Glyph, border SHAPE and text all differ, so no state is carried by colour
   * alone. The two collapses that would be lies are documented on
   * `MODALITY_PRESENTATION` in stores/endpoints.ts, which is the one place
   * these sentences live.
   *
   * A checkbox here would collapse "we asked and it cannot" into "we never
   * asked", and those lead to opposite outcomes: `false` routes the fallback
   * protocol and the step RUNS, `null` makes dispatch REFUSE. That silent
   * downgrade is exactly what standing rule R1 forbids, and it is why
   * `supports_tools` is nullable in the database at all.
   *
   * AND IT OFFERS THE VERB. Every unknown is a question only a probe can
   * answer, so when the host passes `onProbe` the component puts Probe next
   * to the unknowns rather than making the operator hunt for it. It is hidden
   * entirely once there is nothing left to ask.
   *
   * PROP CONTRACT (other lanes import this by path from `../components/endpoint`):
   *   endpoint  required — a `ModelEndpoint` row, straight off the wire
   *   variant   'row'   dense chips for a table cell (default)
   *             'panel' labelled block with every reason, for a detail row,
   *                     the edit modal and the Playground
   *   onProbe   optional () => void — renders the inline Probe affordance
   *   probing   optional boolean — true while that probe is in flight
   */
  import type { ModelEndpoint } from '../../api/types';
  import {
    capabilityCells,
    contextWindowLabel,
    modalityCells,
    unansweredCells,
    MODALITIES_UNREPORTED,
  } from '../../stores/endpoints';

  interface Props {
    endpoint: ModelEndpoint;
    variant?: 'row' | 'panel';
    onProbe?: (() => void) | null;
    probing?: boolean;
  }

  let { endpoint, variant = 'row', onProbe = null, probing = false }: Props = $props();

  let cells = $derived(capabilityCells(endpoint));
  let modalities = $derived(modalityCells(endpoint));
  /**
   * Drives the display off "is there anything to render", NOT off whether the
   * `modalities` key exists. An empty list and a missing key are different
   * causes with the same consequence — nothing to show — and the one thing
   * neither may render as is a blank, because a blank where a modality should
   * be reads as "no".
   */
  let hasModalities = $derived(modalityCells(endpoint).length > 0);
  let unanswered = $derived(unansweredCells(endpoint));
  let contextLabel = $derived(contextWindowLabel(endpoint));
  let contextUnknown = $derived(endpoint.capabilities.context_window === null);

  /**
   * The dense variant drops `text`. Every endpoint that answers at all
   * answers text, so a permanently-green chip there costs table width and
   * carries no information; the panel still shows it, because the panel is
   * where completeness is worth more than density.
   */
  let rowModalities = $derived(modalities.filter((m) => m.key !== 'text'));

  const GLYPH: Record<string, string> = {
    supported: '✓',
    /* Not a ✓. The server accepted the content part and nothing corroborated
       that the model consumed it - a shim that flattens parts into the prompt
       as prose moves the token ledger exactly like a real encoder does. Same
       glyph as `supported` would be the UI making a claim the probe did not. */
    supported_unverified: '≈',
    unsupported: '✗',
    unprobed: '?',
  };

  const TRI_TONE: Record<string, string> = {
    supported: 'good',
    supported_unverified: 'qualified',
    unsupported: 'warn',
    unprobed: 'unknown',
  };

  /** Spelled out rather than string-mangled: "not probed" must never be a
      near-miss of "not supported" produced by an accident of substrings. */
  const TRI_LABEL: Record<string, string> = {
    supported: 'supported',
    supported_unverified: 'accepted, unverified',
    unsupported: 'not supported',
    unprobed: 'not probed',
  };

  let probeTitle = $derived(
    unanswered.length === 0
      ? 'Nothing left to ask.'
      : `Probe this endpoint — unanswered: ${unanswered.map((u) => u.label).join(', ')}.`,
  );
</script>

{#snippet probeButton(dense: boolean)}
  {#if onProbe && unanswered.length > 0}
    <button
      type="button"
      class="probe-btn"
      class:dense
      data-testid="endpoint-cap-probe-btn"
      data-unanswered={unanswered.length}
      disabled={probing}
      title={probeTitle}
      onclick={() => onProbe?.()}
    >
      {probing ? 'Probing…' : dense ? '↻ probe' : `↻ Probe now (${unanswered.length} unanswered)`}
    </button>
  {/if}
{/snippet}

{#if variant === 'panel'}
  <div class="panel" data-testid="endpoint-capabilities-panel" data-variant="panel">
    <div class="group">
      <h4>Protocol</h4>
      <ul class="list">
        {#each cells as cell (cell.key)}
          <li
            class="line tone-{TRI_TONE[cell.state]} outline-{cell.state === 'unprobed'
              ? 'dashed'
              : 'solid'}"
            data-testid="endpoint-cap-{cell.key}"
            data-state={cell.state}
            title={cell.detail}
          >
            <span class="glyph" aria-hidden="true">{GLYPH[cell.state]}</span>
            <span class="name">{cell.label}</span>
            <span class="state" class:italic={cell.state === 'unprobed'}>
              {TRI_LABEL[cell.state] ?? cell.state}
            </span>
            <span class="why">{cell.detail}</span>
          </li>
        {/each}
        <li
          class="line tone-{contextUnknown ? 'unknown' : 'good'} outline-{contextUnknown
            ? 'dashed'
            : 'solid'}"
          data-testid="endpoint-cap-context"
          data-context-known={contextUnknown ? 'false' : 'true'}
        >
          <span class="glyph" aria-hidden="true">{contextUnknown ? '?' : '✓'}</span>
          <span class="name">context</span>
          <span class="state">{contextLabel}</span>
          <span class="why">
            {contextUnknown
              ? 'Neither the operator nor the probe supplied a context window. The harness assumes 8192 tokens and says so, loudly, in the step log.'
              : `Effective window: ${endpoint.capabilities.context_window} tokens (${endpoint.context_window_source ?? 'probe'}).`}
          </span>
        </li>
      </ul>
    </div>

    <div class="group">
      <h4>Input modalities</h4>
      {#if !hasModalities}
        <p class="unreported" data-testid="endpoint-modalities-unreported">
          {MODALITIES_UNREPORTED}
        </p>
      {:else}
        <ul class="list">
          {#each modalities as cell (cell.key)}
            <li
              class="line tone-{cell.presentation.tone} outline-{cell.presentation.outline}"
              data-testid="endpoint-cap-{cell.key}"
              data-state={cell.state}
              data-source={cell.source ?? ''}
              title={cell.detail}
            >
              <span class="glyph" aria-hidden="true">{cell.presentation.glyph}</span>
              <span class="name">
                <span class="icon" aria-hidden="true">{cell.icon}</span>
                <span class:struck={cell.presentation.struck}>{cell.label}</span>
              </span>
              <span
                class="state"
                class:italic={cell.presentation.outline !== 'solid'}
                data-testid="endpoint-cap-{cell.key}-state"
              >
                {cell.presentation.label}
              </span>
              <span class="why">
                {cell.presentation.meaning}
                {#if cell.reason || cell.evidence || cell.source}
                  <span class="provenance" data-testid="endpoint-cap-{cell.key}-provenance">
                    {#if cell.source}<code>{cell.source}</code>{/if}
                    {#if cell.reason}<code>{cell.reason}</code>{/if}
                    {#if cell.caveat}<code>caveat: {cell.caveat}</code>{/if}
                    {#if cell.evidence}<q>{cell.evidence}</q>{/if}
                  </span>
                {/if}
                {#if cell.next}
                  <strong class="next" data-testid="endpoint-cap-{cell.key}-next">
                    {cell.next}
                  </strong>
                {/if}
              </span>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    {#if onProbe && unanswered.length > 0}
      <p class="panel-action">
        {@render probeButton(false)}
        <span class="muted">
          {unanswered.length} unanswered — dispatch refuses rather than guessing, so this stays a
          question until somebody asks the server.
        </span>
      </p>
    {/if}
  </div>
{:else}
  <div class="caps" data-testid="endpoint-capabilities" data-variant="row">
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

    <!-- The modality group. One tight cluster rather than three more full
         chips: at 1280x800 this column has ~60px of slack, and seven wide
         chips would push the whole table back under the pinned actions. -->
    {#if hasModalities}
      <span class="modality-group" data-testid="endpoint-modalities" aria-label="input modalities">
        {#each rowModalities as cell (cell.key)}
          <span
            class="cap mod tone-{cell.presentation.tone} outline-{cell.presentation.outline}"
            data-testid="endpoint-cap-{cell.key}"
            data-state={cell.state}
            data-source={cell.source ?? ''}
            title={`${cell.label}: ${cell.presentation.label}. ${cell.detail}${cell.next ? ' → ' + cell.next : ''}`}
            aria-label={`${cell.label}: ${cell.presentation.label}`}
          >
            <span class="icon" aria-hidden="true">{cell.icon}</span>
            <span class="glyph" aria-hidden="true">{cell.presentation.glyph}</span>
          </span>
        {/each}
      </span>
    {:else}
      <span
        class="cap state-unreported"
        data-testid="endpoint-modalities-unreported"
        title={MODALITIES_UNREPORTED}
      >
        modalities n/a
      </span>
    {/if}

    {@render probeButton(true)}
  </div>
{/if}

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

  /* Qualified: usable, but the probe never corroborated it. Deliberately not
     the `good` colour - the whole point is that a human can tell the two
     apart at a glance without opening the tooltip. */
  .tone-qualified,
  .state-supported_unverified {
    color: var(--warn-fg, #b45309);
    border-style: dashed;
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

  /* Not a capability state: the BACKEND could not answer. Kept visually apart
     from `unprobed` because Probe cannot fix it. */
  .state-unreported {
    color: var(--text-muted);
    background: transparent;
    border: 1px dotted var(--border-color);
    font-style: italic;
    text-decoration: underline dotted;
  }

  .ctx {
    color: var(--text-color);
    font-variant-numeric: tabular-nums;
  }

  .modality-group {
    display: inline-flex;
    gap: 0.15rem;
    align-items: center;
    padding-left: 0.2rem;
    border-left: 1px solid var(--border-color);
  }

  .cap.mod {
    padding: 0.1rem 0.25rem;
    gap: 0.1rem;
  }

  .icon {
    font-size: 0.7rem;
    line-height: 1;
  }

  .probe-btn {
    background: var(--surface-alt);
    border: 1px solid var(--primary-color);
    color: var(--primary-color);
    border-radius: 4px;
    font-size: 0.72rem;
    padding: 0.1rem 0.4rem;
    cursor: pointer;
    white-space: nowrap;
  }

  .probe-btn:hover:not(:disabled) {
    background: var(--hover-color);
  }

  .probe-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .probe-btn.dense {
    font-size: 0.68rem;
  }

  /* --- panel ------------------------------------------------------------- */

  .panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    font-size: 0.78rem;
  }

  .group h4 {
    margin: 0 0 0.3rem;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    font-weight: 600;
  }

  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .line {
    display: grid;
    grid-template-columns: 1.2rem 7rem 8rem 1fr;
    gap: 0.4rem;
    align-items: baseline;
    padding: 0.25rem 0.4rem;
    border-radius: 4px;
    border: 1px solid transparent;
  }

  .line .name {
    display: inline-flex;
    gap: 0.3rem;
    align-items: baseline;
    font-weight: 600;
  }

  .line .state {
    font-weight: 600;
    white-space: nowrap;
  }

  .line .state.italic {
    font-style: italic;
  }

  .line .why {
    color: var(--text-muted);
    line-height: 1.5;
    font-style: normal;
    font-weight: 400;
  }

  .provenance {
    display: block;
    margin-top: 0.15rem;
  }

  .provenance code {
    font-size: 0.68rem;
    background: var(--badge-bg);
    padding: 0 0.2rem;
    border-radius: 3px;
    margin-right: 0.25rem;
  }

  .provenance q {
    font-style: italic;
  }

  .next {
    display: block;
    margin-top: 0.15rem;
    color: var(--text-color);
  }

  .unreported {
    margin: 0;
    padding: 0.4rem 0.5rem;
    border: 1px dotted var(--border-color);
    border-radius: 4px;
    color: var(--text-muted);
    line-height: 1.5;
    max-width: 90ch;
  }

  .panel-action {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    margin: 0;
  }

  .muted {
    color: var(--text-muted);
  }

  @media (max-width: 700px) {
    .line {
      grid-template-columns: 1.2rem 1fr;
    }

    .line .state,
    .line .why {
      grid-column: 2;
    }
  }
  /* -------------------------------------------------------------------------
     Tone (colour) and outline (shape) are SEPARATE channels on purpose: an
     operator who cannot tell amber from red still reads `unsupported` (solid)
     apart from `probe_failed` (dashed), and `unprobed` (dashed) apart from
     `undetectable` (dotted), because the border shape differs too.

     THIS BLOCK MUST STAY LAST. Both `.cap` and `.line` set the shorthand
     `border: 1px solid transparent` for layout stability, and at equal
     specificity the later rule wins. Declared above `.line` these lost, and
     the panel rendered every state with the same invisible border - which
     collapses `unprobed` into `undetectable`, since those two share a colour
     BY DESIGN and the border is the only thing telling them apart.
     ------------------------------------------------------------------------- */
  .tone-good {
    color: var(--success-color);
  }

  .tone-warn {
    color: var(--warning-color);
  }

  .tone-bad {
    color: var(--error-color);
  }

  .tone-unknown {
    color: var(--text-muted);
    background: transparent;
  }

  .tone-inert {
    color: var(--text-muted);
    background: transparent;
    opacity: 0.75;
  }

  .outline-solid {
    border-style: solid;
    border-color: currentColor;
  }

  .outline-dashed {
    border-style: dashed;
    border-color: currentColor;
    font-style: italic;
  }

  .outline-dotted {
    border-style: dotted;
    border-color: currentColor;
    font-style: italic;
  }

  .struck {
    text-decoration: line-through;
  }
</style>

