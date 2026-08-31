<script lang="ts">
  /**
   * Model Endpoints — the registry of self-hosted OpenAI-compatible servers
   * (Milestone 14.3).
   *
   * The page is SNAPSHOT-THEN-DELTA (`stores/endpoints`), the same pattern the
   * runner panel has used since 12.6: one fetch on mount, then
   * `model_endpoint_status` frames. It never polls.
   *
   * Everything here is written to be honest about what is NOT known, because
   * the whole reason this milestone exists is that an inference server is not
   * an agent and the platform has to discover, rather than assume, what each
   * one can do:
   *
   *   - a capability is THREE-state, and "never probed" looks nothing like
   *     "probed and unsupported" (see CapabilityChecks);
   *   - `unpriced` is rendered as an absence and never as $0.00, while
   *     $0.00/hr is rendered as the real claim it is ("owned hardware");
   *   - a runner-local endpoint whose label nobody carries says so IN RED,
   *     before anyone dispatches a step that would wait five minutes and then
   *     fail;
   *   - a probe that could not reach the server is a RED ROW, not a failed
   *     request. "It is down" is a successful observation.
   */
  import { onMount } from 'svelte';
  import type { ModelEndpoint, ModelEndpointCreate, ModelEndpointUpdate } from '../api/types';
  import {
    endpointsStore,
    capabilityCells,
    costIsShared,
    modalityCells,
    modalitiesReported,
    rateLabel,
    runnerShortfall,
  } from '../stores/endpoints';
  import { CapabilityChecks, EndpointHealthDot, EndpointModal } from '../components/endpoint';
  import { formatRelative } from '../utils/time';
  import { describeError } from '../utils/errors';

  // The store's list is the default subscribe value; loading / loaded / error
  // / probing are SIBLING stores on the same object (the runner-panel idiom),
  // so they are bound to locals here rather than reached for through `$store.x`.
  const endpointsLoading = endpointsStore.loading;
  const endpointsLoaded = endpointsStore.loaded;
  const endpointsError = endpointsStore.error;
  const endpointsProbing = endpointsStore.probing;

  let showModal = $state(false);
  let editing = $state<ModelEndpoint | null>(null);
  /** endpoint id -> the last thing an action said about it, good or bad. */
  let notices = $state<Record<string, { tone: 'ok' | 'warn' | 'bad'; text: string }>>({});
  /** Rows the operator has expanded to read probe detail / last error. */
  let expanded = $state<Record<string, boolean>>({});
  let busy = $state<Record<string, boolean>>({});

  onMount(() => {
    void endpointsStore.load();
  });

  function openCreate() {
    editing = null;
    showModal = true;
  }

  function openEdit(endpoint: ModelEndpoint) {
    editing = endpoint;
    showModal = true;
  }

  function note(id: string, tone: 'ok' | 'warn' | 'bad', text: string) {
    notices = { ...notices, [id]: { tone, text } };
  }

  /**
   * The one-line summary a probe writes into the row notice.
   *
   * It names the modality states as well as the protocol ones, because the
   * answer an operator most needs after pressing Probe is which questions the
   * probe actually managed to answer — and a modality that came back
   * `probe_failed` or `undetectable` is precisely the one that would otherwise
   * be mistaken for "no".
   */
  function capabilitySummary(row: ModelEndpoint): string {
    const parts = capabilityCells(row).map((c) => `${c.label}=${c.state}`);
    if (!modalitiesReported(row)) {
      parts.push('modalities=not reported by this backend');
    } else {
      for (const cell of modalityCells(row)) {
        if (cell.key === 'text') continue;
        parts.push(`${cell.label}=${cell.state}`);
      }
    }
    return parts.join(' ');
  }

  async function save(
    payload: ModelEndpointCreate | ModelEndpointUpdate,
    isEdit: boolean,
    probe: boolean,
  ) {
    if (isEdit && editing) {
      const row = await endpointsStore.update(editing.id, payload as ModelEndpointUpdate);
      note(
        row.id,
        row.capabilities.probe_status === 'unprobed' ? 'warn' : 'ok',
        row.capabilities.probe_status === 'unprobed'
          ? 'Saved. The capability record was reset to never-probed because an identity field changed — probe it before dispatching.'
          : 'Saved.',
      );
    } else {
      const response = await endpointsStore.create(payload as ModelEndpointCreate, probe);
      const row = response.endpoint;
      note(
        row.id,
        row.health === 'healthy' ? 'ok' : row.health === 'unprobed' ? 'warn' : 'bad',
        response.detail ?? `Registered and probed: ${row.health}. ` + capabilitySummary(row),
      );
    }
    showModal = false;
    editing = null;
  }

  async function probe(endpoint: ModelEndpoint) {
    busy = { ...busy, [endpoint.id]: true };
    try {
      const response = await endpointsStore.probe(endpoint.id, true);
      const row = response.endpoint;
      if (response.probe_run_id) {
        note(
          endpoint.id,
          'warn',
          `Probing on the runner carrying '${row.runner_label}' — pipeline run ${response.probe_run_id}. ` +
            `A runner-local endpoint is unreachable from the backend by definition, so it is probed from the network position the real step will occupy.`,
        );
      } else if (response.cached) {
        note(endpoint.id, 'warn', response.detail ?? 'Returned the cached record (probed moments ago).');
      } else if (row.capabilities.probe_status === 'unreachable') {
        note(
          endpoint.id,
          'bad',
          `Unreachable: ${row.last_error ?? 'no detail'}. The previous capability record was kept — a rebooting box is not evidence that the model changed.`,
        );
      } else {
        note(
          endpoint.id,
          row.capabilities.probe_status === 'ok' ? 'ok' : 'warn',
          `Probed ${row.capabilities.probe_status}: ` + capabilitySummary(row),
        );
      }
    } catch (e) {
      note(endpoint.id, 'bad', `Probe request failed: ${describeError(e)}`);
    } finally {
      busy = { ...busy, [endpoint.id]: false };
    }
  }

  async function toggleEnabled(endpoint: ModelEndpoint) {
    busy = { ...busy, [endpoint.id]: true };
    try {
      const row = await endpointsStore.update(endpoint.id, { enabled: !endpoint.enabled });
      note(
        row.id,
        row.enabled ? 'ok' : 'warn',
        row.enabled
          ? 'Enabled.'
          : 'Disabled. New steps aimed at it fail at dispatch with a clear reason; runs already in flight are untouched.',
      );
    } catch (e) {
      note(endpoint.id, 'bad', describeError(e));
    } finally {
      busy = { ...busy, [endpoint.id]: false };
    }
  }

  async function remove(endpoint: ModelEndpoint) {
    if (
      !confirm(
        `Delete endpoint '${endpoint.name}'?\n\n` +
          `Historical usage rows keep their gpu_node_id ('${endpoint.gpu_node_id}') and stay ` +
          `priceable, so past cost figures survive. Steps referencing it lose the link.`,
      )
    ) {
      return;
    }
    busy = { ...busy, [endpoint.id]: true };
    try {
      await endpointsStore.remove(endpoint.id);
    } catch (e) {
      // The 409 body names the step runs holding the slots — show it verbatim.
      note(endpoint.id, 'bad', describeError(e));
    } finally {
      busy = { ...busy, [endpoint.id]: false };
    }
  }

  function toggleExpanded(id: string) {
    expanded = { ...expanded, [id]: !expanded[id] };
  }

  function closeModal() {
    showModal = false;
    editing = null;
  }

  /**
   * Escape closes the register/edit dialog.
   *
   * Every other modal in the app does this (CardModal, AgentFileModal,
   * graph/StepConfigModal, debug/DebugRerunModal), so a dialog that ignores
   * Escape breaks a habit the rest of the app teaches; measured before the fix,
   * the only way out was the small "x". The handler lives on the page rather
   * than in EndpointModal because the page owns `showModal`.
   */
  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && showModal) {
      event.stopPropagation();
      closeModal();
    }
  }

  let endpoints = $derived($endpointsStore);
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="page" data-testid="endpoints-page">
  <header class="page-header">
    <div>
      <h1>Model Endpoints</h1>
      <p class="subtitle">
        Self-hosted OpenAI-compatible servers — ollama, vLLM, llama.cpp — on bare metal or a
        rented pod. One row is one <strong>(server, model)</strong> pair, because tool support,
        image and audio input, context window, rate and concurrency are all properties of the
        model on that server. Every capability is <strong>probed, never assumed</strong>, and
        “never asked” is shown as a different fact from “asked, and no”.
      </p>
    </div>
    <div class="header-actions">
      <button
        type="button"
        data-testid="endpoints-refresh-btn"
        onclick={() => endpointsStore.load()}
        disabled={$endpointsLoading}
      >
        {$endpointsLoading ? 'Loading…' : 'Refresh'}
      </button>
      <button type="button" class="primary" data-testid="register-endpoint-btn" onclick={openCreate}>
        + Register endpoint
      </button>
    </div>
  </header>

  {#if $endpointsError}
    <div class="banner bad" data-testid="endpoints-error">
      <span>{$endpointsError}</span>
      <span class="banner-actions">
        <button type="button" onclick={() => endpointsStore.load()}>Retry</button>
        <button type="button" onclick={() => endpointsStore.clearError()}>Dismiss</button>
      </span>
    </div>
  {/if}

  {#if !$endpointsLoaded && $endpointsLoading}
    <p class="muted" data-testid="endpoints-loading">Loading the registry…</p>
  {:else if endpoints.length === 0}
    <div class="empty" data-testid="endpoints-empty">
      <h2>No model endpoints registered</h2>
      <p>
        Register the OpenAI-compatible root of a server you run — for example
        <code>http://192.168.1.50:11434/v1</code> with model
        <code>qwen2.5-coder:32b</code>. LazyAF will probe it immediately and tell you what it can
        actually do: whether it emits real tool calls, whether it streams, whether it reports
        token usage, how big its context window is, and which input modalities it accepts.
      </p>
      <p class="muted">
        Video is listed on every endpoint and can never be green: the OpenAI chat-completions wire
        format has no video content part, so LazyAF cannot send video to <em>any</em> endpoint,
        whatever the model can do. It is shown rather than hidden so the absence has a reason
        attached to it.
      </p>
      <p class="muted">
        There is deliberately no default endpoint. Guessing which GPU to bill is not a recoverable
        mistake.
      </p>
    </div>
  {:else}
    <div class="table-scroll">
      <table data-testid="endpoints-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Reach</th>
            <th>Health</th>
            <th>Capabilities</th>
            <th>Cost basis</th>
            <th>Concurrency</th>
            <th>Enabled</th>
            <th class="right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each endpoints as endpoint (endpoint.id)}
            {@const shortfall = runnerShortfall(endpoint)}
            {@const notice = notices[endpoint.id]}
            <tr
              class:disabled={!endpoint.enabled}
              data-testid="endpoint-row"
              data-endpoint={endpoint.name}
              data-health={endpoint.health}
              data-enabled={endpoint.enabled}
              data-probe-status={endpoint.capabilities.probe_status}
              data-probed-at={endpoint.capabilities.probed_at ?? ''}
            >
              <td>
                <button
                  type="button"
                  class="link name"
                  data-testid="endpoint-name"
                  onclick={() => toggleExpanded(endpoint.id)}
                  title="Show probe detail"
                >
                  {endpoint.name}
                </button>
                <div class="sub" data-testid="endpoint-model">
                  {endpoint.server_kind} · {endpoint.model}
                </div>
                <div class="sub url" title={endpoint.base_url}>{endpoint.base_url}</div>
              </td>

              <td>
                <span class="pill reach-{endpoint.reach}" data-testid="endpoint-reach">
                  {endpoint.reach}
                </span>
                {#if endpoint.reach === 'runner-local'}
                  <div
                    class="sub"
                    class:bad={endpoint.runner_count === 0}
                    data-testid="endpoint-runner-count"
                  >
                    runners: {endpoint.runner_count ?? '?'}
                  </div>
                {:else if endpoint.reach === 'proxy'}
                  <div class="sub warn">inference flows through the backend</div>
                {/if}
              </td>

              <td>
                <EndpointHealthDot
                  health={endpoint.health}
                  age={endpoint.capabilities.probed_at
                    ? formatRelative(endpoint.capabilities.probed_at)
                    : 'never'}
                />
                {#if endpoint.consecutive_failures > 0}
                  <div class="sub bad" data-testid="endpoint-failures">
                    {endpoint.consecutive_failures} consecutive failure(s)
                  </div>
                {/if}
              </td>

              <!-- `onProbe` is what makes an unknown ACTIONABLE where it is
                   read. The button only exists while something is unanswered,
                   so a fully probed row costs no width; it expands the row as
                   well as probing, because "probe failed" and "undetectable"
                   are answers you have to read the reason for, and the reason
                   lives in the detail row. -->
              <td>
                <CapabilityChecks
                  {endpoint}
                  onProbe={() => {
                    expanded = { ...expanded, [endpoint.id]: true };
                    void probe(endpoint);
                  }}
                  probing={busy[endpoint.id] || $endpointsProbing.includes(endpoint.id)}
                />
              </td>

              <td>
                <span
                  class="rate"
                  class:unpriced={!endpoint.priced}
                  data-testid="endpoint-rate"
                  data-priced={endpoint.priced}
                  title={endpoint.priced
                    ? `Costed as rate × container_seconds × gpu_fraction (${endpoint.gpu_fraction}). cost_source = "gpu-node".`
                    : 'No rate is set, so cost_usd stays NULL and cost_source stays "unknown". That is "we do not know", which is different from "it was free".'}
                >
                  {rateLabel(endpoint)}
                </span>
                {#if costIsShared(endpoint)}
                  <div class="sub warn" data-testid="endpoint-cost-shared">
                    shared ×{endpoint.gpu_fraction.toFixed(2)}
                  </div>
                {/if}
              </td>

              <td>
                <span data-testid="endpoint-concurrency">
                  {endpoint.in_flight} / {endpoint.max_concurrency} busy
                </span>
              </td>

              <td>
                <button
                  type="button"
                  class="toggle"
                  class:on={endpoint.enabled}
                  data-testid="endpoint-enabled-toggle"
                  disabled={busy[endpoint.id]}
                  onclick={() => toggleEnabled(endpoint)}
                  title={endpoint.enabled ? 'Disable this endpoint' : 'Enable this endpoint'}
                >
                  {endpoint.enabled ? 'enabled' : 'disabled'}
                </button>
              </td>

              <td class="right actions">
                <button
                  type="button"
                  data-testid="endpoint-probe-btn"
                  disabled={busy[endpoint.id] || $endpointsProbing.includes(endpoint.id)}
                  onclick={() => probe(endpoint)}
                >
                  {$endpointsProbing.includes(endpoint.id) ? 'Probing…' : 'Probe now'}
                </button>
                <button type="button" data-testid="endpoint-edit-btn" onclick={() => openEdit(endpoint)}>
                  Edit
                </button>
                <button
                  type="button"
                  class="danger"
                  data-testid="endpoint-delete-btn"
                  disabled={endpoint.in_flight > 0 || busy[endpoint.id]}
                  title={endpoint.in_flight > 0
                    ? `${endpoint.in_flight} step(s) are holding this endpoint's slots. Wait for them or cancel them first.`
                    : 'Delete this endpoint'}
                  onclick={() => remove(endpoint)}
                >
                  Delete
                </button>
              </td>
            </tr>

            {#if shortfall || endpoint.warning || notice || expanded[endpoint.id]}
              <tr class="detail-row" data-testid="endpoint-detail-row">
                <td colspan="8">
                  {#if shortfall}
                    <p class="detail bad" data-testid="endpoint-runner-shortfall">{shortfall}</p>
                  {/if}
                  {#if endpoint.warning}
                    <p class="detail warn" data-testid="endpoint-url-warning">{endpoint.warning}</p>
                  {/if}
                  {#if notice}
                    <p class="detail {notice.tone}" data-testid="endpoint-notice">{notice.text}</p>
                  {/if}
                  {#if expanded[endpoint.id]}
                    <!-- The same component as the table cell, in its panel
                         variant: one display, two densities. The reasons the
                         chips only hint at in a tooltip are spelled out here,
                         which is where the page already puts probe detail. -->
                    <CapabilityChecks
                      {endpoint}
                      variant="panel"
                      onProbe={() => probe(endpoint)}
                      probing={busy[endpoint.id] || $endpointsProbing.includes(endpoint.id)}
                    />
                    <dl class="probe-detail" data-testid="endpoint-probe-detail">
                      <dt>probed from</dt>
                      <dd>{endpoint.capabilities.probed_from ?? 'never probed'}</dd>
                      <dt>context window</dt>
                      <dd>
                        {endpoint.capabilities.context_window ?? 'unknown — the harness assumes 8192 and says so'}
                        {#if endpoint.context_window_source}
                          <span class="muted">({endpoint.context_window_source})</span>
                        {/if}
                      </dd>
                      <dt>gpu node id</dt>
                      <dd><code>{endpoint.gpu_node_id}</code></dd>
                      <dt>auth</dt>
                      <dd>
                        {endpoint.auth_style}
                        {#if endpoint.auth_secret_ref}
                          · <code>{endpoint.auth_secret_ref}</code>
                          {#if !endpoint.secret_present}
                            <span class="bad" data-testid="endpoint-secret-missing">
                              not set in the backend environment
                            </span>
                          {/if}
                        {/if}
                      </dd>
                      {#if endpoint.last_error}
                        <dt>last error</dt>
                        <dd class="bad" data-testid="endpoint-last-error">{endpoint.last_error}</dd>
                      {/if}
                      {#if Object.keys(endpoint.probe_detail ?? {}).length > 0}
                        <dt>probe detail</dt>
                        <dd><pre>{JSON.stringify(endpoint.probe_detail, null, 2)}</pre></dd>
                      {/if}
                    </dl>
                  {/if}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>

    <p class="footnote">
      Steps select an endpoint by writing <code>model: "endpoint:&lt;name&gt;"</code> with
      <code>agent: openai-harness</code> — the same <code>model</code> field the card picker, the
      playground, the pipeline step form and the experiment matrix already populate, which is what
      lets one matrix mix API and self-hosted models in a single run.
    </p>
  {/if}
</div>

{#if showModal}
  <EndpointModal endpoint={editing} onSave={save} onCancel={closeModal} />
{/if}

<style>
  .page {
    padding: 1.5rem 2rem 3rem;
    overflow-y: auto;
    height: 100%;
  }

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1.25rem;
  }

  h1 {
    margin: 0 0 0.35rem;
    font-size: 1.5rem;
  }

  .subtitle {
    margin: 0;
    max-width: 70ch;
    color: var(--text-muted);
    font-size: 0.85rem;
    line-height: 1.5;
  }

  .header-actions {
    display: flex;
    gap: 0.5rem;
  }

  button {
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    color: var(--text-color);
    border-radius: 6px;
    padding: 0.4rem 0.75rem;
    font-size: 0.82rem;
    cursor: pointer;
  }

  button:hover:not(:disabled) {
    background: var(--hover-color);
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button.primary {
    background: var(--primary-color);
    color: var(--primary-text);
    border-color: var(--primary-color);
    font-weight: 600;
  }

  button.danger:hover:not(:disabled) {
    border-color: var(--error-color);
    color: var(--error-color);
  }

  button.link {
    background: none;
    border: none;
    padding: 0;
    color: var(--primary-color);
    font-weight: 600;
    font-size: 0.95rem;
  }

  .banner {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: center;
    padding: 0.6rem 0.9rem;
    border-radius: 6px;
    margin-bottom: 1rem;
    font-size: 0.85rem;
  }

  .banner.bad {
    background: rgba(243, 139, 168, 0.1);
    border-left: 3px solid var(--error-color);
    color: var(--error-color);
  }

  .banner-actions {
    display: flex;
    gap: 0.4rem;
    flex-shrink: 0;
  }

  .empty {
    border: 1px dashed var(--border-color);
    border-radius: 10px;
    padding: 2rem;
    max-width: 70ch;
  }

  .empty h2 {
    margin-top: 0;
    font-size: 1.05rem;
  }

  .empty p {
    font-size: 0.88rem;
    line-height: 1.6;
  }

  .table-scroll {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }

  /* `white-space: nowrap` made the HEADER the widest thing in four columns —
     "CONCURRENCY" forced 114px for a cell reading "0 / 1 busy", "CAPABILITIES"
     103px for 60px of chips, "COST BASIS" 93px for "$0.35/hr". Measured, the
     table needed 1042px inside an 894px scroller at 1280x800; letting the
     labels wrap onto a second line and tightening the cell padding recovers
     enough that the whole table fits, so nothing is occluded at the width this
     is usually demoed at. */
  th {
    text-align: left;
    padding: 0.6rem 0.5rem;
    background: var(--surface-alt);
    border-bottom: 1px solid var(--border-color);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    vertical-align: bottom;
  }

  td {
    padding: 0.6rem 0.5rem;
    border-bottom: 1px solid var(--border-color);
    vertical-align: top;
  }

  tr.disabled td {
    opacity: 0.5;
  }

  .right {
    text-align: right;
  }

  /* Eight columns do not fit beside a 320px sidebar: measured at 1280x800 the
     table is 1130px inside an 894px scroller, so Probe / Edit / Delete started
     at x=1239 - past the right edge of the window. The row's actions were
     effectively absent unless you found the table's faint inner scrollbar, and
     at 1024 the overflow is 452px. Pinning the last column to the right of the
     scroller keeps the per-row actions on screen at every width; the rest of
     the row still scrolls under it. */
  .actions {
    white-space: nowrap;
    position: sticky;
    right: 0;
    z-index: 1;
    background: var(--bg-color);
    border-left: 1px solid var(--border-color);
    /* At widths too narrow for the whole table the rest of the row scrolls
       UNDER this column. The shadow is what makes that read as a pinned pane
       rather than as content sliced in half. */
    box-shadow: -10px 0 10px -10px rgba(0, 0, 0, 0.9);
  }

  th.right {
    position: sticky;
    right: 0;
    z-index: 2;
    background: var(--surface-alt);
    border-left: 1px solid var(--border-color);
    box-shadow: -10px 0 10px -10px rgba(0, 0, 0, 0.9);
  }

  .actions button {
    padding: 0.4rem 0.6rem;
  }

  .actions button + button {
    margin-left: 0.3rem;
  }

  .sub {
    font-size: 0.72rem;
    color: var(--text-muted);
    margin-top: 0.15rem;
  }

  /* The URL sub-line was the widest thing in the Name column and it already
     truncates with an ellipsis and a `title` tooltip, so trimming it costs a
     hover rather than the information. Together with the wrapping headers this
     is what brings the table under the 894px available at 1280x800, so the
     Enabled toggle stops being the thing that scrolls under the pinned actions
     at the width this is usually demoed at. */
  .sub.url {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    max-width: 20ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .bad {
    color: var(--error-color);
  }

  .warn {
    color: var(--warning-color);
  }

  .muted {
    color: var(--text-muted);
  }

  .pill {
    display: inline-block;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.7rem;
    border: 1px solid var(--border-color);
    background: var(--badge-bg);
  }

  .reach-proxy {
    border-color: var(--warning-color);
    color: var(--warning-color);
  }

  .reach-runner-local {
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .rate {
    font-variant-numeric: tabular-nums;
  }

  .rate.unpriced {
    color: var(--warning-color);
    font-style: italic;
  }

  .toggle {
    font-size: 0.72rem;
    padding: 0.15rem 0.5rem;
  }

  .toggle.on {
    border-color: var(--success-color);
    color: var(--success-color);
  }

  .detail-row td {
    background: var(--surface-alt);
    padding-top: 0.4rem;
  }

  .detail {
    margin: 0 0 0.4rem;
    font-size: 0.8rem;
    line-height: 1.5;
  }

  .detail.ok {
    color: var(--success-color);
  }

  .probe-detail {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.25rem 1rem;
    margin: 0.5rem 0 0;
    font-size: 0.78rem;
  }

  .probe-detail dt {
    color: var(--text-muted);
    text-transform: uppercase;
    font-size: 0.68rem;
    letter-spacing: 0.04em;
  }

  .probe-detail dd {
    margin: 0;
  }

  pre {
    margin: 0;
    font-size: 0.7rem;
    max-height: 14rem;
    overflow: auto;
    background: var(--bg-color);
    padding: 0.5rem;
    border-radius: 4px;
  }

  code {
    font-size: 0.75rem;
    background: var(--badge-bg);
    padding: 0 0.2rem;
    border-radius: 3px;
  }

  .footnote {
    margin-top: 1rem;
    font-size: 0.78rem;
    color: var(--text-muted);
    max-width: 90ch;
    line-height: 1.6;
  }
</style>
