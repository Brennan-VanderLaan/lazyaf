/**
 * Model endpoint store contract — Milestone 14.3 (Agent E test contract 1 & 2).
 *
 * The store is snapshot-then-delta, and BOTH halves are tested, because the
 * regression the 12.6 runner panel actually shipped was the missing half: the
 * deltas worked, the snapshot was deleted, and a reload showed an empty panel
 * over a live fleet. A test that only drove deltas would have gone green over
 * it.
 *
 * The third group is a DRIFT GUARD in the shape `websocket.test.ts` already
 * uses: it greps the BACKEND source for the vocabularies this UI renders
 * (`HEALTH_STATES`, `ENDPOINT_MODEL_PREFIX`) and fails naming the side that
 * moved. Standing rule R3 — one source of truth per wire contract, pinned by
 * a test both sides import — is what makes "the derived health label matches
 * the backend's for all five states" a fact rather than a hope.
 *
 * `modelEndpoints` is mocked at the module boundary (R6: the seam is the real
 * HTTP client the store calls, not a hand-rolled fake store). `ApiError`
 * stays REAL because the store's error text flows through
 * `utils/errors.describeError`, which narrows on it.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import type { EndpointHealth, ModelEndpoint } from '../api/types';
import { ApiError } from '../api/client';

const listMock = vi.fn();
const createMock = vi.fn();
const updateMock = vi.fn();
const deleteMock = vi.fn();
const probeMock = vi.fn();

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  modelEndpoints: {
    list: (...args: unknown[]) => listMock(...args),
    create: (...args: unknown[]) => createMock(...args),
    update: (...args: unknown[]) => updateMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
    probe: (...args: unknown[]) => probeMock(...args),
  },
}));

// Imported AFTER the mock is registered so the store closes over it.
const {
  endpointsStore,
  endpointOptions,
  usableEndpoints,
  busyEndpoints,
  hasEndpoints,
  triState,
  capabilityCells,
  modalityCells,
  modalitiesReported,
  modalityPresentation,
  unansweredCells,
  contextWindowLabel,
  rateLabel,
  costIsShared,
  runnerShortfall,
  toOption,
  healthPresentation,
  endpointModelValue,
  ENDPOINT_MODEL_PREFIX,
  HEALTH_PRESENTATION,
  HEALTH_STATES,
  MODALITY_PRESENTATION,
  MODALITY_ORDER,
  MODALITIES_UNREPORTED,
} = await import('./endpoints');

function makeEndpoint(overrides: Partial<ModelEndpoint> = {}): ModelEndpoint {
  const caps = {
    supports_tools: true,
    supports_streaming: true,
    reports_usage: true,
    supports_images: null,
    supports_audio: null,
    context_window: 32768,
    max_output_tokens: 4096,
    probe_status: 'ok' as const,
    probed_at: '2026-08-30T09:14:22Z',
    probed_from: 'backend',
    probe_age_seconds: 120,
    stale: false,
    // Deliberately EMPTY by default, and the `withModalities` helper below is
    // what fills it. The default fixture is a row registered before modality
    // detection: booleans null, list empty. That is the shape the first load
    // after this milestone actually has, so it is the shape most of this file
    // exercises.
    modalities: [] as ModelEndpoint['capabilities']['modalities'],
    ...(overrides.capabilities ?? {}),
  };
  return {
    id: 'ep-1',
    name: 'local-4090',
    description: null,
    base_url: 'http://172.17.0.1:11434/v1',
    model: 'qwen2.5-coder:32b',
    server_kind: 'ollama',
    auth_style: 'none',
    auth_secret_ref: null,
    auth_header_name: null,
    secret_present: true,
    reach: 'direct',
    runner_label: null,
    runner_count: null,
    rate_usd_hour: '0.000000',
    gpu_node_id: 'endpoint:local-4090',
    gpu_fraction: 1,
    priced: true,
    max_concurrency: 1,
    request_timeout_seconds: 300,
    context_window: null,
    context_window_source: 'probe:ollama',
    max_output_tokens: 4096,
    health: 'healthy',
    probe_detail: {},
    consecutive_failures: 0,
    last_success_at: null,
    last_error: null,
    warning: null,
    enabled: true,
    in_flight: 0,
    created_at: '2026-08-30T09:00:00Z',
    updated_at: '2026-08-30T09:14:22Z',
    ...overrides,
    capabilities: caps,
    pricing: {
      gpu_node_id: 'endpoint:local-4090',
      gpu_fraction: 1,
      priced: true,
      ...(overrides.pricing ?? {}),
    },
  };
}

beforeEach(() => {
  for (const m of [listMock, createMock, updateMock, deleteMock, probeMock]) m.mockReset();
  listMock.mockResolvedValue([]);
  endpointsStore.reset();
});

// -----------------------------------------------------------------------------

describe('snapshot half', () => {
  it('load() populates the store from GET /api/model-endpoints', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({ id: 'a', name: 'alpha' }),
      makeEndpoint({ id: 'b', name: 'beta' }),
    ]);

    await endpointsStore.load();

    expect(get(endpointsStore).map((e) => e.name)).toEqual(['alpha', 'beta']);
    expect(get(endpointsStore.loaded)).toBe(true);
    expect(get(hasEndpoints)).toBe(true);
  });

  it('load() replaces the previous snapshot rather than appending to it', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({ id: 'a', name: 'alpha' }),
      makeEndpoint({ id: 'b', name: 'beta' }),
    ]);
    await endpointsStore.load();

    listMock.mockResolvedValue([makeEndpoint({ id: 'b', name: 'beta' })]);
    await endpointsStore.load();

    expect(get(endpointsStore).map((e) => e.id)).toEqual(['b']);
  });

  it('rows are name-ordered so a probe landing never reshuffles the table', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({ id: '1', name: 'zeta' }),
      makeEndpoint({ id: '2', name: 'alpha' }),
      makeEndpoint({ id: '3', name: 'mid' }),
    ]);
    await endpointsStore.load();
    expect(get(endpointsStore).map((e) => e.name)).toEqual(['alpha', 'mid', 'zeta']);

    endpointsStore.applyDelta({
      id: '1',
      endpoint: makeEndpoint({ id: '1', name: 'zeta', health: 'degraded' }),
    });
    expect(get(endpointsStore).map((e) => e.name)).toEqual(['alpha', 'mid', 'zeta']);
  });

  it('a failed load names the surface and leaves the last known registry visible', async () => {
    listMock.mockResolvedValue([makeEndpoint({ id: 'a', name: 'alpha' })]);
    await endpointsStore.load();

    listMock.mockRejectedValue(new Error('backend down'));
    await endpointsStore.load();

    expect(get(endpointsStore.error)).toBe('Could not load model endpoints: backend down');
    expect(get(endpointsStore).map((e) => e.id)).toEqual(['a']);
  });

  it('reports the real API failure, never the words "Unknown error"', async () => {
    listMock.mockRejectedValue(
      new ApiError(0, 'Cannot reach the LazyAF backend (Failed to fetch)'),
    );
    await endpointsStore.load();

    expect(get(endpointsStore.error)).toContain('Cannot reach the LazyAF backend');
    expect(get(endpointsStore.error)).not.toContain('Unknown error');
  });

  it('clearError() dismisses a read report without a refetch', async () => {
    listMock.mockRejectedValue(new Error('backend down'));
    await endpointsStore.load();
    const before = listMock.mock.calls.length;

    endpointsStore.clearError();

    expect(get(endpointsStore.error)).toBeNull();
    expect(listMock.mock.calls.length).toBe(before);
  });
});

// -----------------------------------------------------------------------------

describe('delta half (model_endpoint_status)', () => {
  it('a delta for an UNKNOWN id is an insert', () => {
    endpointsStore.applyDelta({
      id: 'new-one',
      endpoint: makeEndpoint({ id: 'new-one', name: 'runpod-a100' }),
    });
    expect(get(endpointsStore).map((e) => e.id)).toEqual(['new-one']);
  });

  it('a delta for a KNOWN id updates in place, not as a duplicate', async () => {
    listMock.mockResolvedValue([makeEndpoint({ id: 'a', name: 'alpha' })]);
    await endpointsStore.load();

    endpointsStore.applyDelta({
      id: 'a',
      endpoint: makeEndpoint({
        id: 'a',
        name: 'alpha',
        health: 'degraded',
        in_flight: 2,
        capabilities: { supports_tools: false, probe_status: 'degraded' } as never,
      }),
    });

    const rows = get(endpointsStore);
    expect(rows).toHaveLength(1);
    expect(rows[0].health).toBe('degraded');
    expect(rows[0].in_flight).toBe(2);
    expect(rows[0].capabilities.supports_tools).toBe(false);
  });

  it('a probe delta populates the capability record without a reload', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({
        id: 'a',
        name: 'alpha',
        health: 'unprobed',
        capabilities: {
          supports_tools: null,
          supports_streaming: null,
          reports_usage: null,
          context_window: null,
          probe_status: 'unprobed',
          probed_at: null,
        } as never,
      }),
    ]);
    await endpointsStore.load();
    expect(triState(get(endpointsStore)[0].capabilities.supports_tools)).toBe('unprobed');

    endpointsStore.applyDelta({ id: 'a', endpoint: makeEndpoint({ id: 'a', name: 'alpha' }) });

    const row = get(endpointsStore)[0];
    expect(row.health).toBe('healthy');
    expect(triState(row.capabilities.supports_tools)).toBe('supported');
    expect(row.capabilities.context_window).toBe(32768);
  });

  it('endpoint: null is a DELETE — the row leaves the page', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({ id: 'a', name: 'alpha' }),
      makeEndpoint({ id: 'b', name: 'beta' }),
    ]);
    await endpointsStore.load();

    endpointsStore.applyDelta({ id: 'a', endpoint: null });

    expect(get(endpointsStore).map((e) => e.id)).toEqual(['b']);
  });

  it('enabled: false KEEPS the row (a disabled endpoint is not a deleted one)', async () => {
    listMock.mockResolvedValue([makeEndpoint({ id: 'a', name: 'alpha' })]);
    await endpointsStore.load();

    endpointsStore.applyDelta({
      id: 'a',
      endpoint: makeEndpoint({ id: 'a', name: 'alpha', enabled: false }),
    });

    const rows = get(endpointsStore);
    expect(rows).toHaveLength(1);
    expect(rows[0].enabled).toBe(false);
    // ...and it is still offered in the selectors, disabled, with the reason,
    // because a vanished option is indistinguishable from one never created.
    expect(get(endpointOptions)[0]).toMatchObject({ disabled: true, disabledReason: 'disabled' });
  });

  it('a malformed frame (null, no id, no endpoint key) is dropped', () => {
    endpointsStore.applyDelta(null);
    endpointsStore.applyDelta(undefined);
    endpointsStore.applyDelta({ id: '', endpoint: makeEndpoint() });
    expect(get(endpointsStore)).toHaveLength(0);
  });

  it('a delta does NOT clear an error: it does not repair a missing snapshot', async () => {
    listMock.mockRejectedValue(new Error('backend down'));
    await endpointsStore.load();

    endpointsStore.applyDelta({ id: 'a', endpoint: makeEndpoint({ id: 'a' }) });

    expect(get(endpointsStore)).toHaveLength(1);
    expect(get(endpointsStore.error)).toContain('Could not load model endpoints');
  });
});

// -----------------------------------------------------------------------------

describe('probe action', () => {
  it('applies the 200 record even when the endpoint is DOWN', async () => {
    listMock.mockResolvedValue([makeEndpoint({ id: 'a', name: 'alpha' })]);
    await endpointsStore.load();

    probeMock.mockResolvedValue({
      endpoint: makeEndpoint({
        id: 'a',
        name: 'alpha',
        health: 'unhealthy',
        consecutive_failures: 1,
        last_error: 'connection refused',
        capabilities: { probe_status: 'unreachable' } as never,
      }),
      cached: false,
      probe_run_id: null,
      detail: null,
    });

    const response = await endpointsStore.probe('a');

    expect(response.endpoint.health).toBe('unhealthy');
    expect(get(endpointsStore)[0].last_error).toBe('connection refused');
  });

  it('surfaces the rate-limited cached record rather than pretending it probed', async () => {
    probeMock.mockResolvedValue({
      endpoint: makeEndpoint({ id: 'a' }),
      cached: true,
      probe_run_id: null,
      detail: 'probed 4s ago; returning the cached record',
    });

    const response = await endpointsStore.probe('a');

    expect(response.cached).toBe(true);
    expect(response.detail).toContain('cached');
  });

  it('tracks in-flight probes and clears the flag even when the request throws', async () => {
    probeMock.mockRejectedValue(new ApiError(500, 'HTTP 500'));

    await expect(endpointsStore.probe('a')).rejects.toBeInstanceOf(ApiError);

    expect(get(endpointsStore.probing)).toEqual([]);
  });
});

// -----------------------------------------------------------------------------

describe('CRUD actions keep the map in step with the response', () => {
  it('create() inserts the row the 201 carried, probe record and all', async () => {
    createMock.mockResolvedValue({
      endpoint: makeEndpoint({ id: 'new', name: 'workshop' }),
      cached: false,
      probe_run_id: null,
      detail: null,
    });

    await endpointsStore.create({ name: 'workshop', base_url: 'http://x/v1', model: 'm' });

    expect(get(endpointsStore).map((e) => e.name)).toEqual(['workshop']);
    expect(createMock).toHaveBeenCalledWith(
      { name: 'workshop', base_url: 'http://x/v1', model: 'm' },
      true,
    );
  });

  it('update() replaces the row wholesale', async () => {
    listMock.mockResolvedValue([makeEndpoint({ id: 'a', name: 'alpha' })]);
    await endpointsStore.load();
    updateMock.mockResolvedValue(makeEndpoint({ id: 'a', name: 'alpha', max_concurrency: 4 }));

    await endpointsStore.update('a', { max_concurrency: 4 });

    expect(get(endpointsStore)[0].max_concurrency).toBe(4);
  });

  it('remove() drops the row only after the DELETE succeeded', async () => {
    listMock.mockResolvedValue([makeEndpoint({ id: 'a', name: 'alpha' })]);
    await endpointsStore.load();

    deleteMock.mockRejectedValue(new ApiError(409, "endpoint 'alpha' has 1 step(s) in flight"));
    await expect(endpointsStore.remove('a')).rejects.toBeInstanceOf(ApiError);
    expect(get(endpointsStore)).toHaveLength(1);

    deleteMock.mockResolvedValue(undefined);
    await endpointsStore.remove('a');
    expect(get(endpointsStore)).toHaveLength(0);
  });
});

// -----------------------------------------------------------------------------

describe('selection surface (contract #4: endpoint:<name>)', () => {
  it('emits endpoint:<name> as the model value', () => {
    expect(endpointModelValue('local-4090')).toBe('endpoint:local-4090');
    expect(toOption(makeEndpoint()).value).toBe('endpoint:local-4090');
  });

  it('an unprobed endpoint is offered DISABLED with "probe required", not hidden', () => {
    const option = toOption(
      makeEndpoint({
        health: 'unprobed',
        capabilities: { probe_status: 'unprobed', supports_tools: null } as never,
      }),
    );
    expect(option.disabled).toBe(true);
    expect(option.disabledReason).toBe('probe required');
    expect(option.label).toContain('probe required');
  });

  it('an unreachable endpoint is offered disabled with the reason', () => {
    const option = toOption(makeEndpoint({ health: 'unhealthy' }));
    expect(option).toMatchObject({ disabled: true, disabledReason: 'unreachable' });
  });

  it('usable options sort above the ones needing attention', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({ id: '1', name: 'aaa-broken', health: 'unhealthy' }),
      makeEndpoint({ id: '2', name: 'zzz-good' }),
    ]);
    await endpointsStore.load();

    expect(get(endpointOptions).map((o) => o.name)).toEqual(['zzz-good', 'aaa-broken']);
  });

  it('usableEndpoints excludes disabled, unprobed and unhealthy rows', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({ id: '1', name: 'good' }),
      makeEndpoint({ id: '2', name: 'off', enabled: false }),
      makeEndpoint({
        id: '3',
        name: 'never-asked',
        capabilities: { probe_status: 'unprobed' } as never,
      }),
      makeEndpoint({ id: '4', name: 'down', health: 'unhealthy' }),
      // Degraded is USABLE: it routes the fallback protocol, visibly.
      makeEndpoint({
        id: '5',
        name: 'no-tools',
        health: 'degraded',
        capabilities: { supports_tools: false, probe_status: 'degraded' } as never,
      }),
    ]);
    await endpointsStore.load();

    expect(get(usableEndpoints).map((e) => e.name).sort()).toEqual(['good', 'no-tools']);
  });

  it('busyEndpoints is every row holding a concurrency slot', async () => {
    listMock.mockResolvedValue([
      makeEndpoint({ id: '1', name: 'idle' }),
      makeEndpoint({ id: '2', name: 'working', in_flight: 2 }),
    ]);
    await endpointsStore.load();
    expect(get(busyEndpoints).map((e) => e.name)).toEqual(['working']);
  });
});

// -----------------------------------------------------------------------------

describe('capability presentation: three states, never two', () => {
  it('triState maps true/false/null to three DISTINCT values', () => {
    expect(triState(true)).toBe('supported');
    expect(triState(false)).toBe('unsupported');
    expect(triState(null)).toBe('unprobed');
    expect(triState(undefined)).toBe('unprobed');
    // The whole point: never-probed must not collapse into not-supported.
    expect(triState(null)).not.toBe(triState(false));
  });

  it('a never-probed tools cell says dispatch REFUSES; a false one says fallback', () => {
    const unprobed = capabilityCells(
      makeEndpoint({ capabilities: { supports_tools: null } as never }),
    )[0];
    const unsupported = capabilityCells(
      makeEndpoint({ capabilities: { supports_tools: false } as never }),
    )[0];

    expect(unprobed.state).toBe('unprobed');
    expect(unprobed.detail).toMatch(/refuses/i);
    expect(unsupported.state).toBe('unsupported');
    expect(unsupported.detail).toMatch(/fallback/i);
    expect(unprobed.detail).not.toBe(unsupported.detail);
  });

  it('a null context window is rendered as an assumption, out loud', () => {
    expect(
      contextWindowLabel(makeEndpoint({ capabilities: { context_window: null } as never })),
    ).toBe('ctx unknown (assumes 8k)');
    expect(
      contextWindowLabel(makeEndpoint({ capabilities: { context_window: 32768 } as never })),
    ).toBe('ctx 32k');
  });
});

// -----------------------------------------------------------------------------
// Modalities: SIX states, and the two collapses that would each be a lie.
// -----------------------------------------------------------------------------

/** A capability snapshot carrying a modality list, in the wire's own shape. */
function withModalities(
  entries: Array<{
    modality: string;
    state: string;
    source?: string | null;
    reason?: string | null;
    evidence?: string | null;
    caveat?: string | null;
  }>,
  extra: Record<string, unknown> = {},
): ModelEndpoint {
  return makeEndpoint({
    capabilities: {
      modalities: entries.map((e) => ({
        source: null,
        reason: null,
        evidence: null,
        caveat: null,
        ...e,
      })),
      ...extra,
    } as never,
  });
}

/** The realistic first-load row: probed before modality detection existed. */
const LEGACY_ROW = [
  { modality: 'text', state: 'supported', source: 'wire_format' },
  { modality: 'images', state: 'unprobed' },
  { modality: 'audio', state: 'unprobed' },
  {
    modality: 'video',
    state: 'unrepresentable',
    source: 'wire_format',
    reason: 'wire_format_has_no_video_content_part',
  },
];

describe('modality presentation: no two states collapse into one', () => {
  it('every state has a DISTINCT glyph, outline and sentence', () => {
    const states = Object.keys(MODALITY_PRESENTATION);
    // Counted from the map, not written as a literal. A hardcoded number here
    // fails on the day a state is ADDED, which is exactly when the real
    // property - that no two states look alike - matters most. The sibling
    // test below pins the map against the backend vocabulary, so nothing is
    // lost by not restating the size.
    const n = states.length;
    expect(n).toBeGreaterThanOrEqual(6);

    const glyphs = states.map((k) => modalityPresentation(k).glyph);
    expect(new Set(glyphs).size, `two states share a glyph: ${glyphs.join(' ')}`).toBe(n);

    const meanings = states.map((k) => modalityPresentation(k).meaning);
    expect(new Set(meanings).size, 'two states share a meaning sentence').toBe(n);

    const labels = states.map((k) => modalityPresentation(k).label);
    expect(new Set(labels).size, 'two states share a label').toBe(n);
  });

  it('an unverified acceptance never looks like a proven one', () => {
    const proven = MODALITY_PRESENTATION.supported;
    const unverified = MODALITY_PRESENTATION.supported_unverified;

    // FP-1: a shim that flattens content parts into the prompt as prose moves
    // the token ledger exactly like a real encoder, so the probe's control
    // votes yes for a model that never saw the image. Both remain USABLE -
    // the difference has to be visible, not enforced by a refusal.
    expect(unverified.glyph).not.toBe(proven.glyph);
    expect(unverified.tone).not.toBe(proven.tone);
    expect(unverified.label).not.toBe(proven.label);
    expect(unverified.actionable).toBe(true);
    expect(unverified.next).not.toBeNull();
  });

  it('THE BUG THIS LANE EXISTS FOR: not-probed never renders as not-supported', () => {
    const unprobed = MODALITY_PRESENTATION.unprobed;
    const unsupported = MODALITY_PRESENTATION.unsupported;

    expect(unprobed.glyph).not.toBe(unsupported.glyph);
    expect(unprobed.outline).not.toBe(unsupported.outline);
    expect(unprobed.tone).not.toBe(unsupported.tone);
    expect(unprobed.label).not.toBe(unsupported.label);
    // ...and only one of the two is something a probe can fix.
    expect(unprobed.actionable).toBe(true);
    expect(unsupported.actionable).toBe(false);
    expect(unprobed.next).toMatch(/probe/i);
    expect(unsupported.next).toBeNull();
  });

  it('unprobed and probe_failed are both null-in-the-column but are NOT one cell', () => {
    const unprobed = MODALITY_PRESENTATION.unprobed;
    const failed = MODALITY_PRESENTATION.probe_failed;

    expect(unprobed.glyph).not.toBe(failed.glyph);
    expect(unprobed.tone).not.toBe(failed.tone);
    // Both need a human, and they need DIFFERENT things from them: one says
    // press Probe, the other says read the error before pressing it again.
    expect(unprobed.next).not.toBe(failed.next);
    expect(failed.next).toMatch(/reason/i);
    expect(failed.meaning).toMatch(/never "no"/i);
  });

  it('undetectable is not unsupported: one succeeds while doing nothing', () => {
    const undetectable = MODALITY_PRESENTATION.undetectable;
    const unsupported = MODALITY_PRESENTATION.unsupported;

    expect(undetectable.glyph).not.toBe(unsupported.glyph);
    expect(undetectable.outline).not.toBe(unsupported.outline);
    // The dangerous half has to say the dangerous thing out loud.
    expect(undetectable.meaning).toMatch(/200/);
    expect(undetectable.meaning).toMatch(/discard|vanish/i);
    expect(unsupported.meaning).toMatch(/refus/i);
  });

  it('unrepresentable says the WIRE FORMAT cannot carry it, and offers no probe', () => {
    const cell = MODALITY_PRESENTATION.unrepresentable;
    expect(cell.actionable).toBe(false);
    expect(cell.next).toBeNull();
    expect(cell.struck).toBe(true);
    expect(cell.meaning).toMatch(/wire format/i);
    // It must not read as an observation about this particular server.
    expect(cell.meaning).toMatch(/ANY endpoint/);
  });

  it('colour is never the only channel: tone and outline vary independently', () => {
    // unprobed vs undetectable are the SAME grey on purpose; the border shape
    // is what separates them, so a monochrome screenshot still distinguishes.
    expect(MODALITY_PRESENTATION.unprobed.tone).toBe(MODALITY_PRESENTATION.undetectable.tone);
    expect(MODALITY_PRESENTATION.unprobed.outline).not.toBe(
      MODALITY_PRESENTATION.undetectable.outline,
    );
  });

  it('an unknown state from a newer backend is UNKNOWN, never assumed false', () => {
    const presentation = modalityPresentation('holographic');
    expect(presentation.label).toBe('holographic');
    expect(presentation.tone).toBe('unknown');
    expect(presentation.meaning).toMatch(/refuses rather than assumes/i);
  });
});

describe('modalityCells is a projection of the wire, not a second derivation', () => {
  it('renders the backend list in presentation order', () => {
    const cells = modalityCells(
      withModalities([
        { modality: 'video', state: 'unrepresentable', source: 'wire_format' },
        { modality: 'images', state: 'supported', source: 'wire_probe' },
        { modality: 'text', state: 'supported', source: 'wire_format' },
        { modality: 'audio', state: 'unsupported', source: 'wire_probe' },
      ]),
    );
    expect(cells.map((c) => c.key)).toEqual([...MODALITY_ORDER]);
  });

  it('NEVER reads supports_images: the modality list is the one source', () => {
    // A payload whose boolean and whose list disagree must render the LIST.
    // Reading both would be two derivations of one backend answer.
    const row = makeEndpoint({
      capabilities: {
        supports_images: true,
        modalities: [
          {
            modality: 'images',
            state: 'unprobed',
            source: null,
            reason: null,
            evidence: null,
            caveat: null,
          },
        ],
      } as never,
    });
    expect(modalityCells(row)[0].state).toBe('unprobed');
  });

  it('carries the backend provenance VERBATIM into the tooltip', () => {
    const cell = modalityCells(
      withModalities([
        {
          modality: 'images',
          state: 'unsupported',
          source: 'wire_probe',
          reason: 'http_400',
          evidence: 'this model does not support image input',
        },
      ]),
    )[0];

    expect(cell.source).toBe('wire_probe');
    expect(cell.reason).toBe('http_400');
    expect(cell.detail).toContain('this model does not support image input');
    expect(cell.detail).toContain('http_400');
    // Provenance is appended to the MEANING, not substituted for it.
    expect(cell.detail).toContain(MODALITY_PRESENTATION.unsupported.meaning);
  });

  it('a caveat narrows the claim on screen instead of being dropped', () => {
    const cell = modalityCells(
      withModalities([
        {
          modality: 'images',
          state: 'supported',
          source: 'wire_probe',
          caveat: 'no_usage_no_control',
        },
      ]),
    )[0];
    expect(cell.caveat).toBe('no_usage_no_control');
    expect(cell.detail).toContain('no_usage_no_control');
  });

  it('a wire_format answer says it was never probed and never will be', () => {
    const cell = modalityCells(
      withModalities([{ modality: 'video', state: 'unrepresentable', source: 'wire_format' }]),
    )[0];
    expect(cell.detail).toMatch(/fact about the protocol/i);
    // ...and it must NOT be stamped with a probe time, which would imply the
    // constant was observed against this server.
    expect(cell.detail).not.toContain('Observed');
  });

  it('a probed answer is stamped with WHEN and from WHERE', () => {
    const cell = modalityCells(
      withModalities([{ modality: 'images', state: 'supported', source: 'wire_probe' }]),
    )[0];
    expect(cell.detail).toContain('2026-08-30T09:14:22Z');
    expect(cell.detail).toContain('backend');
  });
});

describe('an unanswered capability is ACTIONABLE where it is read', () => {
  it('the legacy first-load row offers a probe for images and audio', () => {
    const keys = unansweredCells(withModalities(LEGACY_ROW)).map((c) => c.key);

    expect(keys).toContain('images');
    expect(keys).toContain('audio');
    // Video is unanswerable, not unanswered — offering Probe for it would
    // send an operator round a loop that can never terminate.
    expect(keys).not.toContain('video');
    expect(keys).not.toContain('text');
  });

  it('probe_failed and undetectable are actionable; supported and refused are not', () => {
    const keys = unansweredCells(
      withModalities([
        { modality: 'text', state: 'supported', source: 'wire_format' },
        { modality: 'images', state: 'probe_failed', source: 'wire_probe', reason: 'timeout' },
        { modality: 'audio', state: 'undetectable', source: 'wire_probe' },
        { modality: 'video', state: 'unrepresentable', source: 'wire_format' },
      ]),
    ).map((c) => c.key);
    expect(keys.sort()).toEqual(['audio', 'images']);
  });

  it('a fully answered endpoint offers nothing — the button disappears', () => {
    expect(
      unansweredCells(
        withModalities([
          { modality: 'text', state: 'supported', source: 'wire_format' },
          { modality: 'images', state: 'supported', source: 'wire_probe' },
          { modality: 'audio', state: 'unsupported', source: 'wire_probe' },
          { modality: 'video', state: 'unrepresentable', source: 'wire_format' },
        ]),
      ),
    ).toEqual([]);
  });

  it('an unprobed PROTOCOL capability is unanswered too, not just modalities', () => {
    const keys = unansweredCells(withModalities(LEGACY_ROW, { supports_tools: null })).map(
      (c) => c.key,
    );
    expect(keys).toContain('tools');
  });
});

describe('a backend that cannot answer is a FOURTH fact, not "unprobed"', () => {
  it('an ABSENT modalities key renders no cells and reports as unreported', () => {
    const row = makeEndpoint({ capabilities: { modalities: undefined } as never });
    expect(modalitiesReported(row)).toBe(false);
    expect(modalityCells(row)).toEqual([]);
    // ...and nothing about it is actionable: pressing Probe cannot add a
    // field to an older backend's projection.
    expect(unansweredCells(row).map((c) => c.key)).not.toContain('images');
  });

  it('an EMPTY list renders no cells either — same consequence, so same note', () => {
    // The current backend projects one entry per MODALITY_NAMES, so an empty
    // list means something went wrong upstream. It must not fall through to a
    // blank: a blank where a modality should be reads as "no".
    const row = makeEndpoint();
    expect(row.capabilities.modalities).toEqual([]);
    expect(modalityCells(row)).toEqual([]);
    expect(unansweredCells(row).map((c) => c.key)).not.toContain('images');
  });

  it('modalitiesReported still tells the two causes apart for anyone who asks', () => {
    expect(modalitiesReported(makeEndpoint())).toBe(true);
    expect(
      modalitiesReported(makeEndpoint({ capabilities: { modalities: undefined } as never })),
    ).toBe(false);
  });

  it('the message says it is neither "no support" NOR "never probed"', () => {
    expect(MODALITIES_UNREPORTED).toMatch(/not "no image support"/i);
    expect(MODALITIES_UNREPORTED).toMatch(/never probed/i);
    expect(MODALITIES_UNREPORTED).toMatch(/update the backend/i);
  });
});

describe('cost basis: null and 0.00 are different facts and stay different', () => {
  it('unpriced reads "unpriced", never "$0.00"', () => {
    expect(rateLabel(makeEndpoint({ rate_usd_hour: null, priced: false }))).toBe('unpriced');
  });

  it('a zero rate is a REAL claim about owned hardware', () => {
    expect(rateLabel(makeEndpoint({ rate_usd_hour: '0.000000' }))).toBe('$0.00/hr (owned)');
  });

  it('a real rate renders as dollars per hour', () => {
    expect(rateLabel(makeEndpoint({ rate_usd_hour: '1.890000' }))).toBe('$1.89/hr');
  });

  it('cost is flagged SHARED whenever max_concurrency > 1 (gpu_fraction < 1)', () => {
    expect(costIsShared(makeEndpoint({ max_concurrency: 1 }))).toBe(false);
    expect(costIsShared(makeEndpoint({ max_concurrency: 2 }))).toBe(true);
    // Nothing to share when there is no rate to divide.
    expect(
      costIsShared(makeEndpoint({ max_concurrency: 2, rate_usd_hour: null, priced: false })),
    ).toBe(false);
  });
});

describe('runner-local shortfalls are visible BEFORE a step is dispatched', () => {
  it('zero runners carrying the label is called out by name', () => {
    const warning = runnerShortfall(
      makeEndpoint({ reach: 'runner-local', runner_label: 'endpoint:local-4090', runner_count: 0 }),
    );
    expect(warning).toContain('endpoint:local-4090');
    expect(warning).toMatch(/wait, then fail/);
  });

  it('more runners than declared capacity is an amber note, not silence', () => {
    expect(
      runnerShortfall(
        makeEndpoint({ reach: 'runner-local', runner_count: 3, max_concurrency: 2 }),
      ),
    ).toContain('3 runners');
  });

  it('a direct endpoint has no runner story at all', () => {
    expect(runnerShortfall(makeEndpoint({ reach: 'direct', runner_count: 0 }))).toBeNull();
  });
});

// -----------------------------------------------------------------------------
// DRIFT GUARD (R3): the vocabularies this UI renders are the backend's.
// -----------------------------------------------------------------------------

const BACKEND_SOURCES = import.meta.glob('../../../../backend/app/**/*.py', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

function backendSource(suffix: string): string {
  const entry = Object.entries(BACKEND_SOURCES).find(([path]) => path.endsWith(suffix));
  if (!entry) {
    throw new Error(
      `Backend source ${suffix} not found by import.meta.glob. Either the file ` +
        `moved (update this guard) or the glob rotted (fix the glob, do not ` +
        `delete this test).`,
    );
  }
  return entry[1];
}

describe('health vocabulary is the BACKEND vocabulary (contract #1)', () => {
  const source = backendSource('models/model_endpoint.py');

  it('extraction found the backend HEALTH_STATES tuple (regex not rotted)', () => {
    expect(source).toContain('HEALTH_STATES');
  });

  it('FRONTEND DRIFTED if this fails: every backend health state is rendered', () => {
    const block = source.match(/HEALTH_STATES[^=]*=\s*\(([^)]*)\)/);
    expect(block, 'HEALTH_STATES tuple no longer parses out of the backend model').toBeTruthy();
    const backendStates = [...block![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);

    expect(backendStates.length).toBeGreaterThanOrEqual(5);
    for (const state of backendStates) {
      expect(
        Object.keys(HEALTH_PRESENTATION),
        `Backend derives health '${state}' but stores/endpoints.ts has no ` +
          `presentation for it, so the Endpoints page would render a bare ` +
          `string with no meaning. Add it to HEALTH_PRESENTATION.`,
      ).toContain(state);
    }
    // ...and nothing here is invented on this side either.
    for (const state of Object.keys(HEALTH_PRESENTATION)) {
      expect(backendStates, `Frontend renders health '${state}' that the backend never derives`).toContain(
        state,
      );
    }
    expect([...HEALTH_STATES].sort()).toEqual([...backendStates].sort());
  });

  it('every health state carries a MEANING, not just a colour', () => {
    for (const state of HEALTH_STATES) {
      const presentation = healthPresentation(state);
      expect(presentation.meaning.length).toBeGreaterThan(20);
      expect(['bad', 'warn', 'unknown', 'good']).toContain(presentation.tone);
    }
    // degraded and stale are both amber and behave completely differently;
    // a shared sentence would make the dot the only information.
    expect(healthPresentation('degraded').meaning).not.toBe(healthPresentation('stale').meaning);
  });

  it('an unknown state from a newer backend renders honestly instead of blank', () => {
    const presentation = healthPresentation('quantum' as EndpointHealth);
    expect(presentation.label).toBe('quantum');
    expect(presentation.tone).toBe('unknown');
  });
});

describe('the modality vocabulary is the BACKEND vocabulary (M14.6)', () => {
  const source = backendSource('models/model_endpoint.py');

  /** Members of a `NAME: tuple[str, ...] = (...)` declaration. */
  function backendTuple(name: string): string[] {
    const block = source.match(new RegExp(`${name}[^=]*=\\s*\\(([^)]*)\\)`));
    expect(
      block,
      `${name} no longer parses out of models/model_endpoint.py — either it ` +
        `moved (update this guard) or the regex rotted (fix the regex, do ` +
        `not delete this test).`,
    ).toBeTruthy();
    return [...block![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  }

  it('extraction actually found the tuples (regex not rotted)', () => {
    // A floor, not an equality: this test's job is to prove the REGEX still
    // parses the backend tuple, not to restate how many members it has. An
    // equality here turns "the backend gained a state" into a frontend test
    // failure that says nothing about the regex - which is what it did when
    // supported_unverified landed. The sibling test below is the one that
    // fails, correctly and with a useful message, if a state has no
    // presentation.
    expect(backendTuple('MODALITY_STATES').length).toBeGreaterThanOrEqual(6);
    expect(backendTuple('WIRE_MODALITIES').length).toBeGreaterThanOrEqual(3);
  });

  it('FRONTEND DRIFTED if this fails: every backend state has a presentation', () => {
    const backendStates = backendTuple('MODALITY_STATES');
    for (const state of backendStates) {
      expect(
        Object.keys(MODALITY_PRESENTATION),
        `The backend projects modality state '${state}' but ` +
          `stores/endpoints.ts has no presentation for it, so the capability ` +
          `display would render a bare string with no glyph, no outline and ` +
          `no meaning. Add it to MODALITY_PRESENTATION.`,
      ).toContain(state);
    }
    // ...and nothing here is invented on this side either.
    for (const state of Object.keys(MODALITY_PRESENTATION)) {
      expect(
        backendStates,
        `The UI renders modality state '${state}' that the backend never ` +
          `projects.`,
      ).toContain(state);
    }
  });

  it('FRONTEND DRIFTED if this fails: every backend modality is ordered here', () => {
    const backendNames = [
      ...backendTuple('WIRE_MODALITIES'),
      ...backendTuple('UNREPRESENTABLE_MODALITIES'),
    ];
    expect([...MODALITY_ORDER].sort()).toEqual([...backendNames].sort());
  });

  it('video is UNREPRESENTABLE on the backend, so it is never a probe target', () => {
    // If video ever becomes a wire modality this fails, and it SHOULD: the
    // UI's permanent "the wire format cannot carry this" copy would then be
    // a lie, and every sentence about it needs rewriting rather than the
    // chip quietly starting to render a probe button.
    expect(backendTuple('UNREPRESENTABLE_MODALITIES')).toContain('video');
    expect(backendTuple('WIRE_MODALITIES')).not.toContain('video');
    expect(MODALITY_PRESENTATION.unrepresentable.actionable).toBe(false);
  });
});

describe('endpoint: sugar is the BACKEND spelling (contract #4)', () => {
  it('matches services/model_endpoints/resolve.ENDPOINT_MODEL_PREFIX exactly', () => {
    const source = backendSource('services/model_endpoints/resolve.py');
    const match = source.match(/^ENDPOINT_MODEL_PREFIX\s*=\s*"([^"]+)"/m);
    expect(
      match,
      'ENDPOINT_MODEL_PREFIX no longer parses out of resolve.py — the one ' +
        'sugar spelling moved and this UI would emit a model value the ' +
        'resolver cannot parse.',
    ).toBeTruthy();
    expect(ENDPOINT_MODEL_PREFIX).toBe(match![1]);
  });
});

describe('the row shape is the BACKEND projection, key for key (contract #1)', () => {
  const source = backendSource('schemas/model_endpoint.py');

  /** Field names declared on one pydantic class body. */
  function pydanticFields(className: string): string[] {
    const start = source.indexOf(`class ${className}(BaseModel):`);
    expect(start, `class ${className} not found in schemas/model_endpoint.py`).toBeGreaterThan(-1);
    const rest = source.slice(start);
    const end = rest.indexOf('\nclass ', 1);
    const body = end === -1 ? rest : rest.slice(0, end);
    const fields: string[] = [];
    for (const match of body.matchAll(/^ {4}([a-z_][a-z0-9_]*)\s*:\s*[^=\n]/gm)) {
      // `model_config` is pydantic plumbing, not a wire field.
      if (match[1] !== 'model_config') fields.push(match[1]);
    }
    return fields;
  }

  it('extraction actually found the schema (regex not rotted)', () => {
    expect(pydanticFields('ModelEndpointRead').length).toBeGreaterThan(20);
    expect(pydanticFields('EndpointCapabilities').length).toBeGreaterThan(5);
  });

  it('FRONTEND DRIFTED if this fails: every ModelEndpointRead field is typed here', () => {
    const fixture = makeEndpoint();
    for (const field of pydanticFields('ModelEndpointRead')) {
      expect(
        Object.keys(fixture),
        `backend ModelEndpointRead declares '${field}' but the frontend ` +
          `ModelEndpoint interface (api/types.ts) does not carry it, so the ` +
          `Endpoints page cannot render it. Add it on both sides.`,
      ).toContain(field);
    }
  });

  it('BACKEND DRIFTED if this fails: the UI claims no field the API never sends', () => {
    const backendFields = pydanticFields('ModelEndpointRead');
    for (const field of Object.keys(makeEndpoint())) {
      expect(
        backendFields,
        `The frontend ModelEndpoint interface carries '${field}' but ` +
          `ModelEndpointRead does not declare it — the page would render ` +
          `undefined forever.`,
      ).toContain(field);
    }
  });

  it('the capability snapshot matches EndpointCapabilities exactly', () => {
    const backendFields = pydanticFields('EndpointCapabilities').sort();
    const frontendFields = Object.keys(makeEndpoint().capabilities).sort();
    expect(frontendFields).toEqual(backendFields);
  });

  it('the pricing block matches EndpointPricing exactly', () => {
    expect(Object.keys(makeEndpoint().pricing).sort()).toEqual(
      pydanticFields('EndpointPricing').sort(),
    );
  });
});
