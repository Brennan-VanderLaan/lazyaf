import { derived, writable } from 'svelte/store';
import type {
  EndpointHealth,
  EndpointProbeResponse,
  Modality,
  ModalityName,
  ModalityState,
  ModelEndpoint,
  ModelEndpointCreate,
  ModelEndpointStatusFrame,
  ModelEndpointUpdate,
} from '../api/types';
import { modelEndpoints as endpointsApi } from '../api/client';
import { describeError } from '../utils/errors';

/**
 * Model endpoint store — Milestone 14.3. SNAPSHOT FETCH + WEBSOCKET DELTAS,
 * the same shape `stores/runners.ts` has used since 12.6, and for the same
 * reasons:
 *
 *   - `load()` fetches the whole registry once. A fleet of endpoints that is
 *     quietly healthy broadcasts nothing for hours, so a delta-only page is
 *     blank on every reload. (That exact regression shipped once already in
 *     the runner panel; it is not repeated here.)
 *   - `applyDelta()` merges one `model_endpoint_status` frame. The frame body
 *     is byte-identical to one row of the snapshot — both come out of
 *     `schemas/model_endpoint.endpoint_read` — so there is one shape to merge
 *     and no field-by-field reconciliation.
 *
 * The frame carries `endpoint: null` to mean DELETED. That is a different
 * fact from `enabled: false`, and the two are handled differently: a deleted
 * row leaves the page, a disabled row STAYS and is greyed. A disabled
 * endpoint is a deliberate operator state that must remain visible and
 * re-enableable; dropping it would look like the row was deleted.
 *
 * Health is NOT derived here. The backend's `ModelEndpoint.health` property
 * is the one definition (probe_status + probe age + consecutive_failures) and
 * this module renders it. A second derivation on this side would be a second
 * writer that drifts from the first — precisely what that property's
 * docstring refuses.
 */

// -----------------------------------------------------------------------------
// Vocabulary (mirrors backend/app/models/model_endpoint.py)
// -----------------------------------------------------------------------------

/**
 * Cross-agent contract #4: `endpoint:` is the ONE sugar spelling for naming a
 * self-hosted model in the `model` field that all four selection surfaces
 * already populate (card, playground, pipeline step, experiment matrix).
 *
 * It is defined backend-side in
 * `services/model_endpoints/resolve.ENDPOINT_MODEL_PREFIX` and PARSED only by
 * `resolve_step_endpoint`. This constant is the PRODUCER half — the selectors
 * emit it and nothing on this side ever parses it back, so there is no second
 * parser to drift.
 */
export const ENDPOINT_MODEL_PREFIX = 'endpoint:';

/** `model` value for a step that should run against `endpoint`. */
export function endpointModelValue(name: string): string {
  return `${ENDPOINT_MODEL_PREFIX}${name}`;
}

/** The agent vocabulary entry every self-hosted endpoint dispatches under. */
export const HARNESS_AGENT = 'openai-harness';

/**
 * The five health states, in worst-to-best order. Ordering is load-bearing:
 * the page sorts unhealthy endpoints to the top, because the row an operator
 * needs to see is the broken one.
 */
export const HEALTH_STATES: readonly EndpointHealth[] = [
  'unhealthy',
  'unprobed',
  'degraded',
  'stale',
  'healthy',
];

export interface HealthPresentation {
  label: string;
  /** 'bad' | 'warn' | 'unknown' | 'good' — drives the dot colour. */
  tone: 'bad' | 'warn' | 'unknown' | 'good';
  /** One sentence saying what this state MEANS for dispatch. */
  meaning: string;
}

/**
 * Every health state says what it means for a step, because "degraded" and
 * "stale" are both amber and they behave completely differently: a degraded
 * endpoint RUNS (down the fallback protocol), a stale one RUNS AND
 * RE-PROBES, and an unprobed one REFUSES. Colour alone cannot carry that.
 */
export const HEALTH_PRESENTATION: Record<EndpointHealth, HealthPresentation> = {
  healthy: {
    label: 'healthy',
    tone: 'good',
    meaning: 'Probed and reachable. Steps dispatch normally.',
  },
  stale: {
    label: 'stale',
    tone: 'warn',
    meaning:
      'The capability record is older than 24h. Steps still RUN — dispatch warns and re-probes in the background.',
  },
  degraded: {
    label: 'degraded',
    tone: 'warn',
    meaning:
      'Reachable, but something the probe checked failed. Degraded is USABLE: no tool support routes the fallback protocol, and no usage block routes an honest token-blind row.',
  },
  unhealthy: {
    label: 'unhealthy',
    tone: 'bad',
    meaning:
      'Unreachable, or three consecutive failures. Dispatch REFUSES this endpoint and names the last error.',
  },
  unprobed: {
    label: 'never probed',
    tone: 'unknown',
    meaning:
      'Nobody has asked this server what it can do. Dispatch REFUSES until it is probed — a thirty-minute agent step is not the place to discover the model cannot tool-call.',
  },
};

export function healthPresentation(health: EndpointHealth | string): HealthPresentation {
  return (
    HEALTH_PRESENTATION[health as EndpointHealth] ?? {
      label: String(health),
      tone: 'unknown',
      meaning: 'Unrecognised health state — the backend vocabulary has moved ahead of this UI.',
    }
  );
}

// -----------------------------------------------------------------------------
// Capability presentation — the three-state rule
// -----------------------------------------------------------------------------

export type TriState = 'supported' | 'unsupported' | 'unprobed';

/**
 * `null` is NOT "assume no".
 *
 * A boolean rendered as a checkbox collapses "we asked and it cannot" into
 * "we never asked", and those two facts lead to opposite outcomes: `false`
 * routes the no-tools fallback protocol and the step RUNS, while `null`
 * makes dispatch REFUSE. Every capability cell therefore renders three
 * distinct states and never a bare unchecked box.
 */
export function triState(value: boolean | null | undefined): TriState {
  if (value === true) return 'supported';
  if (value === false) return 'unsupported';
  return 'unprobed';
}

export interface CapabilityCell {
  key: 'tools' | 'stream' | 'usage';
  label: string;
  state: TriState;
  /** What this state means for a step run against this endpoint. */
  detail: string;
}

export function capabilityCells(endpoint: ModelEndpoint): CapabilityCell[] {
  const caps = endpoint.capabilities;
  const tools = triState(caps.supports_tools);
  const stream = triState(caps.supports_streaming);
  const usage = triState(caps.reports_usage);
  return [
    {
      key: 'tools',
      label: 'tools',
      state: tools,
      detail:
        tools === 'supported'
          ? 'Emits real tool_calls. The harness runs in tools mode.'
          : tools === 'unsupported'
            ? 'No tools — the harness runs the fenced-block fallback protocol.'
            : 'Never probed. Dispatch refuses until this is answered.',
    },
    {
      key: 'stream',
      label: 'stream',
      state: stream,
      detail:
        stream === 'supported'
          ? 'Server-sent events work.'
          : stream === 'unsupported'
            ? 'No streaming; turns arrive whole.'
            : 'Never probed.',
    },
    {
      key: 'usage',
      label: 'usage',
      state: usage,
      detail:
        usage === 'supported'
          ? 'Returns a usage block, so token counts and node-priced cost are possible.'
          : usage === 'unsupported'
            ? 'Reports no usage block — token counts will be NULL, never zero.'
            : 'Never probed.',
    },
  ];
}

// -----------------------------------------------------------------------------
// Modality presentation — six states, and the two collapses that would be lies
// -----------------------------------------------------------------------------

/**
 * Presentation order. `text` is listed because the backend answers it and a
 * list that silently omitted a modality would be a fourth way to hide one;
 * the dense table variant filters it out (every endpoint takes text, so a
 * green "text" chip in a 60px cell is pure noise), the panel shows it.
 */
export const MODALITY_ORDER: readonly ModalityName[] = ['text', 'images', 'audio', 'video'];

export const MODALITY_LABEL: Record<ModalityName, string> = {
  text: 'text',
  images: 'images',
  audio: 'audio',
  video: 'video',
};

/** Icon for the modality itself — orthogonal to the state glyph. */
export const MODALITY_ICON: Record<ModalityName, string> = {
  text: '¶',
  images: '👁',
  audio: '🔊',
  video: '🎞',
};

export interface ModalityPresentation {
  /**
   * One glyph per state, all six distinct. Colour NEVER carries the state on
   * its own — `unsupported` (amber) and `probe_failed` (red) are two colours
   * a red-green-blind operator reads as one, and `unprobed` vs `undetectable`
   * are the same grey by design.
   */
  glyph: string;
  tone: 'good' | 'warn' | 'bad' | 'unknown' | 'inert';
  /** Border shape, the non-colour channel: solid=answered, dashed=null, dotted=200-but-useless. */
  outline: 'solid' | 'dashed' | 'dotted';
  /** Struck-through label: this one can never be answered, so it is not pending. */
  struck: boolean;
  /** Two or three words, for the panel's state column. */
  label: string;
  /** What this state MEANS for a step that attaches this modality. */
  meaning: string;
  /**
   * The next thing a human should DO, or null when there is nothing to do.
   * Non-null is what puts a Probe button on screen next to the cell.
   */
  next: string | null;
  /** True when a probe could change this answer. Drives the inline action. */
  actionable: boolean;
}

/**
 * THE TABLE THIS LANE EXISTS FOR.
 *
 * Two collapses would each be a lie, and they are the ones to check for in
 * review:
 *
 *   unprobed vs probe_failed — both are `null` in the column and both REFUSE
 *     at dispatch, but one says "press Probe" and the other says "the probe
 *     ran and broke; read the reason before pressing it again". Rendering
 *     them alike turns a broken endpoint into a paperwork task.
 *
 *   undetectable vs unsupported — `unsupported` is a positive refusal you can
 *     quote back ("HTTP 400: this model does not support image input").
 *     `undetectable` is a request that SUCCEEDS while doing nothing: the
 *     server took the image, returned 200, and the prompt token count did not
 *     move. That is the more dangerous of the two and the one R1 exists to
 *     surface, so it gets its own glyph, its own outline and its own sentence.
 *
 * And the one that matters on day one: every endpoint registered before the
 * modality probe shipped reads `unprobed`. That is the COMMON case on first
 * load, not an edge case, which is why it is dashed and italic rather than a
 * bare empty cell.
 */
export const MODALITY_PRESENTATION: Record<ModalityState, ModalityPresentation> = {
  supported: {
    glyph: '✓',
    tone: 'good',
    outline: 'solid',
    struck: false,
    label: 'supported',
    meaning:
      'The endpoint accepted this content part, and where a usage block made it measurable the input measurably entered the prompt. It is NOT a claim that the model is any good at it.',
    next: null,
    actionable: false,
  },
  supported_unverified: {
    glyph: '≈',
    tone: 'qualified',
    outline: 'dashed',
    struck: false,
    label: 'accepted, unverified',
    meaning:
      'The endpoint ACCEPTED this content part, but nothing corroborated that the model consumed it — no usage block moved, or the control was unavailable. A shim that flattens content parts into the prompt as prose looks identical from here, so this is deliberately not a plain ✓. It still dispatches: the doubt belongs in front of the human choosing, not in a refusal that would read as “this endpoint cannot do images” when we do not know that.',
    next: 'Re-probe once the server reports usage, or send one attachment by hand and read the reply.',
    actionable: true,
  },
  unsupported: {
    glyph: '✗',
    tone: 'warn',
    outline: 'solid',
    struck: false,
    label: 'not supported',
    meaning:
      'Probed, and the server REFUSED this content part outright. A step that attaches one here fails at dispatch with the server’s own words — it is not silently stripped.',
    next: null,
    actionable: false,
  },
  unprobed: {
    glyph: '?',
    tone: 'unknown',
    outline: 'dashed',
    struck: false,
    label: 'not probed',
    meaning:
      'Nobody has asked. This is NOT "not supported": it is the state every endpoint registered before modality detection shipped is in. Dispatch refuses an attachment rather than guessing.',
    next: 'Probe this endpoint.',
    actionable: true,
  },
  probe_failed: {
    glyph: '!',
    tone: 'bad',
    outline: 'dashed',
    struck: false,
    label: 'probe failed',
    meaning:
      'Somebody asked and the ASKING broke — a timeout, a 5xx, or the probe deadline ran out before this question was reached. A failed probe is UNKNOWN, never "no".',
    next: 'Read the reason below first; re-probing a box that timed out usually just times out again.',
    actionable: true,
  },
  undetectable: {
    glyph: '~',
    tone: 'unknown',
    outline: 'dotted',
    struck: false,
    label: 'undetectable',
    meaning:
      'Asked, and the answer does not decide it: the server returned 200 but the input did not change the prompt token count, so it was almost certainly discarded. The request SUCCEEDS and the input vanishes. Treat as unsupported until you have verified it by hand.',
    next: 'Verify by hand against this server, or pick an endpoint whose chip is green.',
    actionable: true,
  },
  unrepresentable: {
    glyph: '⊘',
    tone: 'inert',
    outline: 'solid',
    struck: true,
    label: 'not expressible',
    meaning:
      'The OpenAI chat-completions wire format has no content part for this, so LazyAF cannot send it to ANY endpoint, whatever the model can do. Nothing to probe: this is a property of the protocol, not of this server. (vLLM’s `video_url` is a vendor extension LazyAF does not speak; frame-sampling into image parts is images.)',
    next: null,
    actionable: false,
  },
};

export function modalityPresentation(state: ModalityState | string): ModalityPresentation {
  return (
    MODALITY_PRESENTATION[state as ModalityState] ?? {
      glyph: '·',
      tone: 'unknown',
      outline: 'dashed',
      struck: false,
      label: String(state),
      meaning:
        'Unrecognised modality state — the backend vocabulary has moved ahead of this UI. Treated as unknown, which refuses rather than assumes.',
      next: 'Update the frontend: stores/endpoints.MODALITY_PRESENTATION has no entry for this state.',
      actionable: false,
    }
  );
}

export interface ModalityCell {
  /** Doubles as the testid suffix: `endpoint-cap-<key>`. */
  key: ModalityName | string;
  label: string;
  icon: string;
  state: ModalityState | string;
  presentation: ModalityPresentation;
  /** The backend's provenance, rendered verbatim rather than paraphrased. */
  source: string | null;
  reason: string | null;
  evidence: string | null;
  caveat: string | null;
  /** Tooltip / panel body: the meaning, then how we came to believe it. */
  detail: string;
  next: string | null;
  actionable: boolean;
}

/**
 * The backend answered NOTHING about modalities — either its capability
 * projection carries no `modalities` key at all (a backend older than
 * modality detection) or it carries an empty one.
 *
 * This is a FOURTH kind of "we do not know" and it deliberately does not get
 * folded into `unprobed`: pressing Probe cannot fix it, so offering Probe
 * would send an operator round a loop that can never terminate. It is also
 * not allowed to render as a BLANK, which is the one rendering that reads as
 * "no image support".
 *
 * A current backend always projects one entry per `MODALITY_NAMES`, so this
 * is defensive rather than routine — which is exactly why it must not fail
 * silently when it does happen.
 */
export const MODALITIES_UNREPORTED =
  'This backend reported no modality answers: its capability projection carries no `modalities` entries. That is not "no image support" and not "never probed" — this backend has no way to answer. Probing will not change it; update the backend.';

/**
 * True when the payload carried a modality list AT ALL — the precise
 * predicate, kept apart from "is there anything to render". The DISPLAY keys
 * off the cell count instead, so an empty list cannot render as a blank.
 */
export function modalitiesReported(endpoint: ModelEndpoint): boolean {
  return Array.isArray(endpoint?.capabilities?.modalities);
}

function toModalityCell(entry: Modality, endpoint: ModelEndpoint): ModalityCell {
  const presentation = modalityPresentation(entry.state);
  const name = entry.modality;
  const provenance: string[] = [];
  if (entry.source === 'wire_format') {
    provenance.push('Not probed and never will be: this is a fact about the protocol.');
  } else if (entry.source) {
    provenance.push(`Source: ${entry.source}.`);
  }
  if (entry.reason) provenance.push(`Reason: ${entry.reason}.`);
  if (entry.evidence) provenance.push(`Server said: ${entry.evidence}`);
  if (entry.caveat) provenance.push(`Caveat: ${entry.caveat}.`);
  if (entry.source && entry.source !== 'wire_format') {
    const probedAt = endpoint.capabilities?.probed_at;
    const probedFrom = endpoint.capabilities?.probed_from;
    if (probedAt) provenance.push(`Observed ${probedAt} from ${probedFrom ?? 'unknown host'}.`);
  }
  return {
    key: name,
    label: MODALITY_LABEL[name] ?? String(name),
    icon: MODALITY_ICON[name] ?? '·',
    state: entry.state,
    presentation,
    source: entry.source ?? null,
    reason: entry.reason ?? null,
    evidence: entry.evidence ?? null,
    caveat: entry.caveat ?? null,
    detail: [presentation.meaning, ...provenance].join(' '),
    next: presentation.next,
    actionable: presentation.actionable,
  };
}

/**
 * The modality cells, in presentation order.
 *
 * A PROJECTION of `capabilities.modalities` and nothing else. It never
 * consults `supports_images` / `supports_audio`: those are the same fact in a
 * narrower shape, and reading both would be two derivations of one backend
 * answer — the drift `ModelEndpoint.health` already refuses on the backend
 * side. An empty array means the backend did not answer (see
 * `MODALITIES_UNREPORTED`); it does not mean "no modalities".
 */
export function modalityCells(endpoint: ModelEndpoint): ModalityCell[] {
  const list = endpoint?.capabilities?.modalities;
  if (!Array.isArray(list)) return [];
  const rank = new Map(MODALITY_ORDER.map((m, i) => [m as string, i]));
  return [...list]
    .filter((entry) => entry && typeof entry.modality === 'string')
    .sort((a, b) => (rank.get(a.modality) ?? 99) - (rank.get(b.modality) ?? 99))
    .map((entry) => toModalityCell(entry, endpoint));
}

/**
 * Every cell — capability AND modality — whose answer a probe could still
 * change. This is what puts "Probe now" beside the unknowns instead of only
 * at the far right of the row: an operator who has just read "not probed"
 * should not have to go hunting for the verb.
 */
export function unansweredCells(endpoint: ModelEndpoint): Array<{ key: string; label: string }> {
  const out: Array<{ key: string; label: string }> = [];
  for (const cell of capabilityCells(endpoint)) {
    if (cell.state === 'unprobed') out.push({ key: cell.key, label: cell.label });
  }
  for (const cell of modalityCells(endpoint)) {
    if (cell.actionable) out.push({ key: String(cell.key), label: cell.label });
  }
  return out;
}

/**
 * `null` context window is rendered as an assumption, out loud. A blank cell
 * would read as "small" or as a rendering bug; the harness genuinely assumes
 * 8192 in this case and the operator has to know that is happening.
 */
export const DEFAULT_ASSUMED_CONTEXT = 8192;

export function contextWindowLabel(endpoint: ModelEndpoint): string {
  const window = endpoint.capabilities.context_window;
  if (window === null || window === undefined) {
    return `ctx unknown (assumes ${DEFAULT_ASSUMED_CONTEXT / 1024}k)`;
  }
  if (window >= 1024 && window % 1024 === 0) return `ctx ${window / 1024}k`;
  if (window >= 1000) return `ctx ${Math.round(window / 1024)}k`;
  return `ctx ${window}`;
}

/**
 * The cost-basis label. `null` and `0.00` are DIFFERENT and stay different:
 * `0.00/hr` is a real claim ("owned hardware, marginal cash cost") that
 * produces `cost_source = "gpu-node"`, and `unpriced` is an absence that
 * produces `cost_source = "unknown"` and a null cost. Collapsing them is the
 * failure mode decision 4 exists to prevent.
 */
export function rateLabel(endpoint: ModelEndpoint): string {
  if (endpoint.rate_usd_hour === null) return 'unpriced';
  const rate = Number(endpoint.rate_usd_hour);
  if (Number.isNaN(rate)) return 'unpriced';
  if (rate === 0) return '$0.00/hr (owned)';
  return `$${rate.toFixed(2)}/hr`;
}

/**
 * `gpu_fraction = 1/max_concurrency`, so any endpoint with a cap above 1
 * attributes a FRACTION of the node's hourly rate to each step. That is a
 * deliberate under-attribution convention, and a cost number produced under
 * it must be labelled rather than quietly presented as the node's full cost.
 */
export function costIsShared(endpoint: ModelEndpoint): boolean {
  return endpoint.max_concurrency > 1 && endpoint.priced;
}

/**
 * A `runner-local` endpoint whose label nobody carries cannot run anything;
 * a step aimed at it sits until NO_RUNNER_TIMEOUT. Saying so on the row makes
 * the fact visible BEFORE a step is dispatched rather than 300 seconds after.
 */
export function runnerShortfall(endpoint: ModelEndpoint): string | null {
  if (endpoint.reach !== 'runner-local') return null;
  const count = endpoint.runner_count;
  if (count === null || count === undefined) return null;
  if (count === 0) {
    return `no connected runner carries label '${endpoint.runner_label ?? `endpoint:${endpoint.name}`}' — a step aimed here will wait, then fail`;
  }
  if (count > endpoint.max_concurrency) {
    return `${count} runners carry this endpoint's label but it declares capacity for ${endpoint.max_concurrency}; on runner-local the RUNNER count is the real concurrency`;
  }
  return null;
}

// -----------------------------------------------------------------------------
// Selection — what the four "choose an agent" surfaces render
// -----------------------------------------------------------------------------

export interface EndpointOption {
  /** The `model` field value: `endpoint:<name>`. */
  value: string;
  name: string;
  label: string;
  /** Disabled options are still LISTED, with the reason. */
  disabled: boolean;
  disabledReason: string | null;
  title: string;
}

/**
 * One endpoint as a `<select>` option.
 *
 * An unusable endpoint is rendered DISABLED AND VISIBLE with its reason,
 * never filtered out. A missing option is indistinguishable from an endpoint
 * that was never registered, and an operator who cannot see why their
 * endpoint is absent will re-register it rather than probe it.
 */
export function toOption(endpoint: ModelEndpoint): EndpointOption {
  let disabledReason: string | null = null;
  if (!endpoint.enabled) {
    disabledReason = 'disabled';
  } else if (endpoint.capabilities.probe_status === 'unprobed') {
    disabledReason = 'probe required';
  } else if (endpoint.health === 'unhealthy') {
    disabledReason = 'unreachable';
  }
  const suffix = disabledReason ? ` — ${disabledReason}` : '';
  return {
    value: endpointModelValue(endpoint.name),
    name: endpoint.name,
    label: `${endpoint.name} (${endpoint.model})${suffix}`,
    disabled: disabledReason !== null,
    disabledReason,
    title: `${endpoint.base_url} · ${endpoint.health} · ${rateLabel(endpoint)}`,
  };
}

// -----------------------------------------------------------------------------
// The store
// -----------------------------------------------------------------------------

function sortEndpoints(list: ModelEndpoint[]): ModelEndpoint[] {
  // Name-ordered, stable. The table must not reshuffle every time a probe
  // lands or an in-flight count ticks, or a busy fleet becomes unreadable.
  return [...list].sort((a, b) => a.name.localeCompare(b.name));
}

function createEndpointsStore() {
  const byId = new Map<string, ModelEndpoint>();
  const { subscribe, set } = writable<ModelEndpoint[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);
  /** True once a snapshot has landed — tells "no endpoints" from "not asked yet". */
  const loaded = writable(false);
  /** Endpoint ids with a probe in flight from THIS page. Drives the spinner. */
  const probing = writable<string[]>([]);

  function publish() {
    set(sortEndpoints([...byId.values()]));
  }

  function upsert(endpoint: ModelEndpoint) {
    if (!endpoint || !endpoint.id) return;
    byId.set(endpoint.id, endpoint);
    publish();
  }

  /** Fetch the whole registry. Called on mount and after a socket reconnect. */
  async function load() {
    loading.set(true);
    error.set(null);
    try {
      const rows = await endpointsApi.list();
      byId.clear();
      for (const row of rows) byId.set(row.id, row);
      loaded.set(true);
      publish();
    } catch (e) {
      // Prefixed: `ApiError.message` is the server's own sentence and on its
      // own does not say WHICH surface is broken.
      error.set(`Could not load model endpoints: ${describeError(e)}`);
    } finally {
      loading.set(false);
    }
  }

  /**
   * Merge one `model_endpoint_status` frame.
   *
   *   endpoint: null   -> DELETE (the row is gone)
   *   unknown id       -> INSERT (registered since the snapshot)
   *   known id         -> REPLACE (the frame is a full projection, not a patch)
   *
   * Deliberately does NOT clear `error`: a delta proves the socket is up but
   * does not repair a snapshot that never landed, and a page that silently
   * drops the warning while listing a partial registry is the quiet lie R1
   * forbids. Recovery is `load()`, or the page's explicit Retry.
   */
  function applyDelta(frame: ModelEndpointStatusFrame | null | undefined) {
    if (!frame || !frame.id) return;
    if (frame.endpoint === null || frame.endpoint === undefined) {
      byId.delete(frame.id);
      publish();
      return;
    }
    // Trust the frame's own id over the envelope's: they are the same value
    // by construction, and keying on the row keeps the map self-consistent
    // if they ever are not.
    byId.set(frame.endpoint.id || frame.id, frame.endpoint);
    publish();
  }

  function clearError() {
    error.set(null);
  }

  async function create(data: ModelEndpointCreate, probe: boolean = true) {
    const response: EndpointProbeResponse = await endpointsApi.create(data, probe);
    upsert(response.endpoint);
    return response;
  }

  async function update(id: string, data: ModelEndpointUpdate) {
    const row = await endpointsApi.update(id, data);
    upsert(row);
    return row;
  }

  async function remove(id: string) {
    await endpointsApi.delete(id);
    byId.delete(id);
    publish();
  }

  /**
   * Probe one endpoint.
   *
   * The response is applied directly rather than waited for over the socket:
   * the WS delta is the path for probes started ELSEWHERE, and a page that
   * only updated from the socket would show nothing at all when the socket
   * is down. Both paths write the same projection, so applying both is
   * idempotent rather than racy.
   *
   * Never throws for "the endpoint is down" — that is a 200 with a red
   * record. It throws only when the REQUEST failed.
   */
  async function probeEndpoint(id: string, force: boolean = false) {
    probing.update((ids) => (ids.includes(id) ? ids : [...ids, id]));
    try {
      const response = await endpointsApi.probe(id, force);
      upsert(response.endpoint);
      return response;
    } finally {
      probing.update((ids) => ids.filter((x) => x !== id));
    }
  }

  /** Test/teardown hook: drop everything without touching the network. */
  function reset() {
    byId.clear();
    loaded.set(false);
    error.set(null);
    probing.set([]);
    publish();
  }

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },
    loaded: { subscribe: loaded.subscribe },
    probing: { subscribe: probing.subscribe },
    load,
    applyDelta,
    clearError,
    create,
    update,
    remove,
    probe: probeEndpoint,
    reset,
  };
}

export const endpointsStore = createEndpointsStore();

/**
 * Every endpoint as a selectable option, in the order the selectors render.
 *
 * Usable options first, then the ones that need attention, each carrying its
 * reason. Disabled rows sink rather than vanish (see `toOption`).
 */
export const endpointOptions = derived(endpointsStore, ($endpoints) => {
  const options = $endpoints.map(toOption);
  return [...options].sort((a, b) => {
    if (a.disabled !== b.disabled) return a.disabled ? 1 : -1;
    return a.name.localeCompare(b.name);
  });
});

/** True when there is at least one endpoint to offer. Gates the optgroup. */
export const hasEndpoints = derived(endpointsStore, ($endpoints) => $endpoints.length > 0);

/** Endpoints an operator can actually dispatch to right now. */
export const usableEndpoints = derived(endpointsStore, ($endpoints) =>
  $endpoints.filter(
    (e) =>
      e.enabled &&
      e.capabilities.probe_status !== 'unprobed' &&
      e.health !== 'unhealthy',
  ),
);

/** Endpoints holding at least one concurrency slot right now. */
export const busyEndpoints = derived(endpointsStore, ($endpoints) =>
  $endpoints.filter((e) => e.in_flight > 0),
);
