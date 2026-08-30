import { derived, writable } from 'svelte/store';
import type {
  EndpointHealth,
  EndpointProbeResponse,
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
