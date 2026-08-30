<script lang="ts">
  /**
   * Register / edit one model endpoint.
   *
   * THE FORM HOLDS NO SECRET VALUE AND SAYS SO. The auth field is a
   * REFERENCE — the NAME of an environment variable on the backend — with the
   * mandatory `LAZYAF_ENDPOINT_` prefix rendered as a fixed, unwritable affix
   * so the allowlist is visible rather than a 422 waiting to happen. LazyAF
   * has no secret-at-rest story (no encryption key, no KMS, SQLite backups are
   * plain files, and the operator API is unauthenticated), so a stored key
   * would be a new class of exposure introduced for the convenience of one
   * text input.
   *
   * `auth_style: none` is the DEFAULT and a first-class case, not an escape
   * hatch: LAN ollama and vLLM behind a firewall genuinely have no key, and a
   * form that makes "no auth" the exceptional branch is a form that grows a
   * fake key.
   *
   * On EDIT, changing base_url / model / server_kind / any auth field resets
   * the capability record to `unprobed` server-side. The form states that
   * before the operator saves, because the consequence — dispatch refuses the
   * endpoint until it is re-probed — is not something to discover afterwards.
   */
  import type {
    EndpointAuthStyle,
    EndpointReach,
    EndpointServerKind,
    ModelEndpoint,
    ModelEndpointCreate,
    ModelEndpointUpdate,
  } from '../../api/types';
  import { untrack } from 'svelte';
  import { describeError } from '../../utils/errors';

  interface Props {
    /** null = register a new endpoint. */
    endpoint?: ModelEndpoint | null;
    /** `probe` is meaningful on CREATE only; an edit never implies a probe. */
    onSave: (
      payload: ModelEndpointCreate | ModelEndpointUpdate,
      isEdit: boolean,
      probe: boolean,
    ) => Promise<void>;
    onCancel: () => void;
  }

  let { endpoint = null, onSave, onCancel }: Props = $props();

  const SECRET_PREFIX = 'LAZYAF_ENDPOINT_';

  const AUTH_STYLES: EndpointAuthStyle[] = ['none', 'bearer', 'header'];

  const SERVER_KINDS: { value: EndpointServerKind; label: string }[] = [
    { value: 'ollama', label: 'ollama' },
    { value: 'vllm', label: 'vLLM' },
    { value: 'llamacpp', label: 'llama.cpp' },
    { value: 'lmstudio', label: 'LM Studio' },
    { value: 'other', label: 'other' },
  ];

  const REACHES: { value: EndpointReach; label: string; hint: string }[] = [
    {
      value: 'direct',
      label: 'direct',
      hint: 'The step container calls the endpoint itself. base_url is written from the CONTAINER’s network position.',
    },
    {
      value: 'runner-local',
      label: 'runner-local',
      hint: 'The step is pinned to a runner on the box that hosts the model. This is the mode that makes NAT’d home hardware work — nothing has to be reachable from the backend.',
    },
    {
      value: 'proxy',
      label: 'proxy',
      hint: 'The BACKEND makes the call on the container’s behalf. Inference traffic flows through the backend and the backend becomes a bottleneck for it. The one advantage: the endpoint key never reaches the container.',
    },
  ];

  /**
   * The modal is mounted fresh for each open, so its inputs are seeded ONCE
   * from the row and then owned by the form. `untrack` says that out loud
   * rather than leaving twenty `state_referenced_locally` warnings for a
   * reader to decide about: re-deriving a field the operator is mid-edit
   * would silently discard their typing the moment a WS delta landed for
   * this endpoint.
   */
  const initial = untrack(() => endpoint);
  const isEdit = initial !== null;

  let name = $state(initial?.name ?? '');
  let description = $state(initial?.description ?? '');
  let baseUrl = $state(initial?.base_url ?? '');
  let model = $state(initial?.model ?? '');
  let serverKind = $state<EndpointServerKind>(
    (initial?.server_kind as EndpointServerKind) ?? 'ollama',
  );
  let authStyle = $state<EndpointAuthStyle>((initial?.auth_style as EndpointAuthStyle) ?? 'none');
  let secretSuffix = $state(
    initial?.auth_secret_ref?.startsWith(SECRET_PREFIX)
      ? initial.auth_secret_ref.slice(SECRET_PREFIX.length)
      : '',
  );
  let authHeaderName = $state(initial?.auth_header_name ?? '');
  let reach = $state<EndpointReach>((initial?.reach as EndpointReach) ?? 'direct');
  let runnerLabel = $state(initial?.runner_label ?? '');
  let rate = $state(initial?.rate_usd_hour ?? '');
  let maxConcurrency = $state(initial?.max_concurrency ?? 1);
  let requestTimeout = $state(initial?.request_timeout_seconds ?? 300);
  let contextWindow = $state(
    initial?.context_window === null || initial?.context_window === undefined
      ? ''
      : String(initial.context_window),
  );
  let maxOutputTokens = $state(
    initial?.max_output_tokens === null || initial?.max_output_tokens === undefined
      ? ''
      : String(initial.max_output_tokens),
  );
  let probeOnSave = $state(true);

  let submitting = $state(false);
  let error = $state<string | null>(null);

  /** The identity fields whose change invalidates every capability observation. */
  let capabilityInvalidated = $derived(
    isEdit &&
      (baseUrl !== initial!.base_url ||
        model !== initial!.model ||
        serverKind !== initial!.server_kind ||
        authStyle !== initial!.auth_style ||
        secretRef() !== initial!.auth_secret_ref ||
        (authHeaderName || null) !== initial!.auth_header_name),
  );

  let baseUrlWarning = $derived(
    baseUrl.trim() !== '' && !baseUrl.trim().replace(/\/+$/, '').endsWith('/v1')
      ? 'This does not end in /v1. LazyAF sends requests to <base_url>/chat/completions exactly as written and never rewrites the URL — that may be what you want behind a reverse proxy, or it may be a 404 waiting to happen.'
      : null,
  );

  let effectiveRunnerLabel = $derived(runnerLabel.trim() || (name ? `endpoint:${name}` : ''));

  function secretRef(): string | null {
    if (authStyle === 'none') return null;
    const suffix = secretSuffix.trim().toUpperCase();
    return suffix ? `${SECRET_PREFIX}${suffix}` : null;
  }

  function numberOrNull(value: string): number | null {
    const trimmed = value.trim();
    if (trimmed === '') return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
  }

  function validate(): string | null {
    if (!name.trim()) return 'A name is required — it is the handle every other surface uses.';
    if (!/^[a-z0-9][a-z0-9-]{0,38}$/.test(name.trim())) {
      return 'Name must be lowercase letters, digits and hyphens, starting with a letter or digit, at most 39 characters.';
    }
    if (!baseUrl.trim()) return 'A base URL is required.';
    if (!model.trim()) return 'A model id is required — it is the id sent in the request body.';
    if (authStyle !== 'none' && !secretSuffix.trim()) {
      return `auth_style '${authStyle}' needs the NAME of a backend environment variable (the value is never stored).`;
    }
    if (authStyle === 'header' && !authHeaderName.trim()) {
      return "auth_style 'header' needs a header name, e.g. x-api-key.";
    }
    return null;
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const invalid = validate();
    if (invalid) {
      error = invalid;
      return;
    }

    const payload: ModelEndpointCreate = {
      name: name.trim(),
      description: description.trim() || null,
      base_url: baseUrl.trim(),
      model: model.trim(),
      server_kind: serverKind,
      auth_style: authStyle,
      auth_secret_ref: secretRef(),
      auth_header_name: authStyle === 'header' ? authHeaderName.trim() : null,
      reach,
      runner_label: reach === 'runner-local' ? effectiveRunnerLabel || null : null,
      // "" means UNPRICED (cost_source stays "unknown"); "0" means owned
      // hardware at zero marginal cash cost, which is a real, different claim.
      rate_usd_hour: rate.trim() === '' ? null : rate.trim(),
      max_concurrency: Number(maxConcurrency) || 1,
      request_timeout_seconds: Number(requestTimeout) || 300,
      context_window: numberOrNull(contextWindow),
      max_output_tokens: numberOrNull(maxOutputTokens),
    };

    submitting = true;
    error = null;
    try {
      await onSave(payload, isEdit, probeOnSave);
    } catch (e) {
      error = describeError(e);
    } finally {
      submitting = false;
    }
  }
</script>

<div
  class="backdrop"
  role="presentation"
  onclick={(e) => {
    if (e.target === e.currentTarget) onCancel();
  }}
>
  <div class="modal" role="dialog" aria-modal="true" aria-label="Model endpoint" data-testid="endpoint-modal">
    <header>
      <h2>{isEdit ? `Edit ${initial!.name}` : 'Register a model endpoint'}</h2>
      <button type="button" class="icon" data-testid="endpoint-modal-close" onclick={onCancel}>×</button>
    </header>

    <form onsubmit={submit}>
      <p class="preamble">
        One row is one <strong>(server, model)</strong> pair. ollama serving two models on one box
        is two endpoints, because tool support, context window, rate and concurrency are all
        properties of the MODEL on that server.
      </p>

      {#if error}
        <div class="error" data-testid="endpoint-modal-error">{error}</div>
      {/if}

      <div class="row">
        <label class="field">
          <span>Name</span>
          <input
            data-testid="endpoint-name-input"
            type="text"
            bind:value={name}
            placeholder="local-4090"
            disabled={isEdit}
          />
          <small>
            {#if isEdit}
              The name is the handle other surfaces already reference; it cannot be changed here.
            {:else}
              Steps select it as <code>model: "endpoint:{name || '<name>'}"</code>.
            {/if}
          </small>
        </label>

        <label class="field">
          <span>Server kind</span>
          <select data-testid="endpoint-server-kind-select" bind:value={serverKind}>
            {#each SERVER_KINDS as kind (kind.value)}
              <option value={kind.value}>{kind.label}</option>
            {/each}
          </select>
          <small>Forensics and probe hints only — never behaviour. Only <code>ollama</code> changes anything (it enables <code>/api/show</code> context discovery).</small>
        </label>
      </div>

      <label class="field">
        <span>Base URL</span>
        <input
          data-testid="endpoint-base-url-input"
          type="text"
          bind:value={baseUrl}
          placeholder="http://192.168.1.50:11434/v1"
        />
        {#if baseUrlWarning}
          <small class="warn" data-testid="endpoint-base-url-warning">{baseUrlWarning}</small>
        {:else}
          <small>The OpenAI-compatible root, <strong>including</strong> the version segment.</small>
        {/if}
      </label>

      <label class="field">
        <span>Model id</span>
        <input
          data-testid="endpoint-model-input"
          type="text"
          bind:value={model}
          placeholder="qwen2.5-coder:32b"
        />
        <small>Exactly the id this server expects in the request body.</small>
      </label>

      <label class="field">
        <span>Description</span>
        <input data-testid="endpoint-description-input" type="text" bind:value={description} placeholder="optional" />
      </label>

      <fieldset data-testid="endpoint-reach-fieldset">
        <legend>Reach — who makes the HTTP call</legend>
        {#each REACHES as option (option.value)}
          <label class="radio">
            <input
              type="radio"
              name="reach"
              value={option.value}
              data-testid="endpoint-reach-{option.value}"
              checked={reach === option.value}
              onchange={() => (reach = option.value)}
            />
            <span class="radio-label">{option.label}</span>
            <small>{option.hint}</small>
          </label>
        {/each}
        {#if reach === 'runner-local'}
          <label class="field indent">
            <span>Runner label</span>
            <input
              data-testid="endpoint-runner-label-input"
              type="text"
              bind:value={runnerLabel}
              placeholder={name ? `endpoint:${name}` : 'endpoint:<name>'}
            />
            <small>
              The step is forced remote by an injected <code>requires: &#123;has: ["{effectiveRunnerLabel || 'endpoint:<name>'}"]&#125;</code>.
              Set <code>LAZYAF_RUNNER_LABELS=has={effectiveRunnerLabel || 'endpoint:<name>'}</code> on the box that hosts the model.
            </small>
          </label>
        {/if}
      </fieldset>

      <fieldset data-testid="endpoint-auth-fieldset">
        <legend>Authentication</legend>
        <p class="no-secret" data-testid="endpoint-no-secret-notice">
          This form never holds a secret <em>value</em>. You name an environment variable on the
          backend; LazyAF resolves it at dispatch and delivers it to the container through the
          0600 consume-once step config file. Nothing here is written to the database.
        </p>
        {#each AUTH_STYLES as style (style)}
          <label class="radio">
            <input
              type="radio"
              name="auth_style"
              value={style}
              data-testid="endpoint-auth-{style}"
              checked={authStyle === style}
              onchange={() => (authStyle = style)}
            />
            <span class="radio-label">{style}</span>
            <small>
              {#if style === 'none'}
                No header at all. The default, and a first-class case — LAN ollama and vLLM behind a firewall genuinely have no key.
              {:else if style === 'bearer'}
                <code>Authorization: Bearer &lt;secret&gt;</code>
              {:else}
                A custom header, e.g. <code>x-api-key: &lt;secret&gt;</code>
              {/if}
            </small>
          </label>
        {/each}

        {#if authStyle !== 'none'}
          <label class="field indent">
            <span>Backend environment variable</span>
            <div class="affixed">
              <span class="affix" data-testid="endpoint-secret-prefix">{SECRET_PREFIX}</span>
              <input
                data-testid="endpoint-secret-suffix-input"
                type="text"
                bind:value={secretSuffix}
                placeholder="LOCAL_4090"
                autocomplete="off"
              />
            </div>
            <small>
              The prefix is fixed and enforced server-side. Without the allowlist a row could
              reference <code>ANTHROPIC_API_KEY</code> and exfiltrate the platform's own
              credentials into a container the operator does not control.
            </small>
            {#if isEdit && initial!.auth_secret_ref && !initial!.secret_present}
              <small class="bad" data-testid="endpoint-secret-missing">
                <code>{initial!.auth_secret_ref}</code> is not set in the backend environment.
                Dispatch will fail naming this variable — which is better than burning thirty
                seconds of container start to reach an opaque 401.
              </small>
            {/if}
          </label>

          {#if authStyle === 'header'}
            <label class="field indent">
              <span>Header name</span>
              <input
                data-testid="endpoint-auth-header-input"
                type="text"
                bind:value={authHeaderName}
                placeholder="x-api-key"
              />
            </label>
          {/if}
        {/if}
      </fieldset>

      <div class="row">
        <label class="field">
          <span>Rate ($/hour)</span>
          <input
            data-testid="endpoint-rate-input"
            type="text"
            bind:value={rate}
            placeholder="leave blank for unpriced"
          />
          <small>
            Blank = <strong>unpriced</strong> → <code>cost_source: unknown</code> and a null cost.
            <code>0</code> = owned hardware, zero marginal cash cost → a REAL
            <code>$0.000000</code> with <code>cost_source: gpu-node</code>. Those are different
            claims and LazyAF keeps them different.
          </small>
        </label>

        <label class="field">
          <span>Max concurrency</span>
          <input
            data-testid="endpoint-concurrency-input"
            type="number"
            min="1"
            max="64"
            bind:value={maxConcurrency}
          />
          <small>
            Defaults to 1. One ollama process serving four requests on one GPU does not go 4×
            faster — it goes about 1× with 4× the latency and a real chance of an OOM that kills
            all four. Above 1, each step is costed at <code>1/{maxConcurrency || 1}</code> of the
            node rate and the UI labels its cost <strong>shared</strong>.
          </small>
        </label>
      </div>

      <div class="row">
        <label class="field">
          <span>Context window override</span>
          <input
            data-testid="endpoint-context-window-input"
            type="number"
            min="1"
            bind:value={contextWindow}
            placeholder="blank = let the probe decide"
          />
          <small>Authoritative when set; beats anything the probe discovers.</small>
        </label>

        <label class="field">
          <span>Max output tokens</span>
          <input
            data-testid="endpoint-max-output-input"
            type="number"
            min="1"
            bind:value={maxOutputTokens}
            placeholder="blank = 1024 at use time"
          />
        </label>

        <label class="field">
          <span>Request timeout (s)</span>
          <input
            data-testid="endpoint-timeout-input"
            type="number"
            min="1"
            max="3600"
            bind:value={requestTimeout}
          />
          <small>Per HTTP request, not per step. 300 because a cold ollama loading a 32B model can take a minute on the first call.</small>
        </label>
      </div>

      {#if capabilityInvalidated}
        <div class="warn-block" data-testid="endpoint-capability-reset-warning">
          Saving this change resets the capability record to <strong>never probed</strong>: a
          capability observed against a different model, URL or credential is not evidence about
          this one. Dispatch will refuse the endpoint until you probe it again.
        </div>
      {/if}

      {#if !isEdit}
        <label class="checkbox">
          <input type="checkbox" data-testid="endpoint-probe-on-save" bind:checked={probeOnSave} />
          <span>
            Probe immediately (recommended). Registering without probing leaves the endpoint
            <strong>unprobed</strong>, and dispatch refuses an unprobed endpoint rather than
            discovering at minute thirty that the model cannot tool-call.
          </span>
        </label>
      {/if}

      <footer>
        <button type="button" class="secondary" data-testid="endpoint-cancel-btn" onclick={onCancel}>
          Cancel
        </button>
        <button type="submit" class="primary" data-testid="endpoint-submit-btn" disabled={submitting}>
          {#if submitting}
            {isEdit ? 'Saving…' : probeOnSave ? 'Registering and probing…' : 'Registering…'}
          {:else}
            {isEdit ? 'Save' : probeOnSave ? 'Register and probe' : 'Register'}
          {/if}
        </button>
      </footer>
    </form>
  </div>
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 2rem 1rem;
    overflow-y: auto;
    z-index: 100;
  }

  .modal {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    width: min(760px, 100%);
    max-height: calc(100vh - 4rem);
    display: flex;
    flex-direction: column;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1rem 1.25rem;
    border-bottom: 1px solid var(--border-color);
  }

  header h2 {
    margin: 0;
    font-size: 1.1rem;
  }

  .icon {
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 1.4rem;
    cursor: pointer;
    line-height: 1;
  }

  form {
    padding: 1.25rem;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .preamble {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .row {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
  }

  .row > .field {
    flex: 1 1 200px;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .field > span {
    font-size: 0.85rem;
    font-weight: 600;
  }

  .indent {
    margin: 0.5rem 0 0 1.5rem;
  }

  input[type='text'],
  input[type='number'],
  select {
    background: var(--input-bg);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    padding: 0.5rem 0.6rem;
    font-size: 0.9rem;
    width: 100%;
  }

  small {
    font-size: 0.72rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  small.warn {
    color: var(--warning-color);
  }

  small.bad {
    color: var(--error-color);
  }

  fieldset {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.75rem 1rem 1rem;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  legend {
    font-size: 0.85rem;
    font-weight: 600;
    padding: 0 0.4rem;
  }

  .radio {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.15rem 0.5rem;
    align-items: baseline;
    cursor: pointer;
  }

  .radio input {
    grid-row: 1;
  }

  .radio-label {
    font-size: 0.9rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  .radio small {
    grid-column: 2;
  }

  .no-secret {
    margin: 0;
    font-size: 0.78rem;
    color: var(--text-muted);
    border-left: 3px solid var(--primary-color);
    padding-left: 0.6rem;
  }

  .affixed {
    display: flex;
    align-items: stretch;
  }

  .affix {
    background: var(--badge-bg);
    border: 1px solid var(--border-color);
    border-right: none;
    border-radius: 6px 0 0 6px;
    padding: 0.5rem 0.5rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.82rem;
    color: var(--text-muted);
    white-space: nowrap;
    display: flex;
    align-items: center;
  }

  .affixed input {
    border-radius: 0 6px 6px 0;
  }

  .checkbox {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    font-size: 0.78rem;
    color: var(--text-muted);
  }

  .error {
    background: rgba(243, 139, 168, 0.12);
    border-left: 3px solid var(--error-color);
    color: var(--error-color);
    padding: 0.6rem 0.75rem;
    border-radius: 4px;
    font-size: 0.85rem;
  }

  .warn-block {
    background: rgba(249, 226, 175, 0.1);
    border-left: 3px solid var(--warning-color);
    color: var(--warning-color);
    padding: 0.6rem 0.75rem;
    border-radius: 4px;
    font-size: 0.82rem;
  }

  footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
    padding-top: 0.5rem;
    border-top: 1px solid var(--border-color);
  }

  button.primary,
  button.secondary {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-size: 0.9rem;
    cursor: pointer;
    border: 1px solid var(--border-color);
  }

  button.primary {
    background: var(--primary-color);
    color: var(--primary-text);
    border-color: var(--primary-color);
    font-weight: 600;
  }

  button.secondary {
    background: transparent;
    color: var(--text-color);
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  code {
    font-size: 0.72rem;
    background: var(--badge-bg);
    padding: 0 0.2rem;
    border-radius: 3px;
  }
</style>
