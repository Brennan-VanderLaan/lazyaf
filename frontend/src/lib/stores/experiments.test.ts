/**
 * Experiments store tests (Phase 12.6.5).
 *
 * The load-bearing assertions here are the guardrail ones: the launch gate
 * cannot be satisfied by a stale estimate, and money / rates never degrade
 * into a confident-looking zero. Everything else is list and frame plumbing.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';

vi.mock('../api/client', () => ({
  experiments: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    dryRun: vi.fn(),
    estimate: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    launch: vi.fn(),
    abort: vi.fn(),
    resume: vi.fn(),
    results: vi.fn(),
    leaderboard: vi.fn(),
  },
}));

import {
  experimentsStore,
  draftKey,
  estimateIsFresh,
  cellCount,
  cellsDone,
  formatUsd,
  formatRate,
  formatDuration,
} from './experiments';
import { experiments as experimentsApi } from '../api/client';
import type {
  ExperimentCreate,
  ExperimentSummary,
  ExperimentDetail,
  ExperimentCell,
  ExperimentEstimate,
  ExperimentLaunchResponse,
  Leaderboard,
} from '../api/types';

function makeLaunchResponse(
  overrides: Partial<ExperimentLaunchResponse> = {}
): ExperimentLaunchResponse {
  return {
    id: 'exp-1',
    status: 'running',
    cells_created: 6,
    dispatched: 2,
    estimated_cost_usd: '1.20',
    estimate_basis: 'historical-median',
    warnings: [],
    ...overrides,
  };
}

function makeDraft(overrides: Partial<ExperimentCreate> = {}): ExperimentCreate {
  return {
    name: 'opus vs haiku',
    description: '',
    target_type: 'card',
    target_id: 'card-1',
    repo_id: 'repo-1',
    matrix: {
      models: [
        { agent: 'mock', model: 'mock-a', label: 'a' },
        { agent: 'mock', model: 'mock-b', label: 'b' },
      ],
      prompts: [{ prompt_template_id: null, label: 'default' }],
      repeat: 3,
    },
    verify: null,
    budget_usd: '5.00',
    max_concurrency: 2,
    push_branches: false,
    ...overrides,
  };
}

function makeEstimate(overrides: Partial<ExperimentEstimate> = {}): ExperimentEstimate {
  return {
    cells: 6,
    models: 2,
    prompts: 1,
    repeat: 3,
    runs: 6,
    estimated_cost_usd: '1.20',
    estimate_basis: 'historical-median',
    per_variant: [],
    budget_usd: '5.00',
    within_budget: true,
    budget_enforced_at_dispatch: true,
    warnings: [],
    ...overrides,
  };
}

function makeSummary(overrides: Partial<ExperimentSummary> = {}): ExperimentSummary {
  return {
    id: 'exp-1',
    name: 'opus vs haiku',
    description: '',
    target_type: 'card',
    target_id: 'card-1',
    repo_id: 'repo-1',
    matrix: makeDraft().matrix,
    verify: null,
    budget_usd: '5.00',
    max_concurrency: 2,
    cell_timeout: 1800,
    push_branches: false,
    status: 'running',
    estimated_cost_usd: '1.20',
    estimate_basis: 'historical-median',
    budget_overrun_usd: '0.00',
    created_by: null,
    created_at: '2026-08-30T00:00:00Z',
    updated_at: '2026-08-30T00:00:01Z',
    launched_at: '2026-08-30T00:00:01Z',
    completed_at: null,
    cells_total: 6,
    by_status: { pending: 4, running: 2 },
    spend_usd: '0.40',
    cost_coverage: 1,
    stalled: false,
    ...overrides,
  } as ExperimentSummary;
}

function makeCell(overrides: Partial<ExperimentCell> = {}): ExperimentCell {
  return {
    id: 'cell-1',
    experiment_id: 'exp-1',
    cell_index: 0,
    variant_index: 0,
    agent: 'mock',
    model: 'mock-a',
    prompt_template_id: null,
    prompt_version: null,
    label: 'a / default',
    repeat_index: 0,
    pipeline_run_id: null,
    status: 'running',
    error: null,
    started_at: '2026-08-30T00:00:02Z',
    completed_at: null,
    created_at: '2026-08-30T00:00:00Z',
    cost_usd: null,
    cost_coverage: null,
    wall_clock_ms: null,
    input_tokens: null,
    output_tokens: null,
    tests_passed: 0,
    tests_failed: 0,
    tests_skipped: 0,
    ...overrides,
  };
}

function makeDetail(overrides: Partial<ExperimentDetail> = {}): ExperimentDetail {
  return {
    ...makeSummary(),
    cells: [makeCell()],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  experimentsStore.clear();
});

// -----------------------------------------------------------------------------
// The guardrail
// -----------------------------------------------------------------------------

describe('launch gate: an estimate is only valid for the draft that produced it', () => {
  it('draftKey changes when a cost-bearing field changes', () => {
    const base = makeDraft();
    expect(draftKey(base)).toBe(draftKey(makeDraft()));

    // Every axis that multiplies the bill moves the key.
    expect(draftKey({ ...base, matrix: { ...base.matrix, repeat: 4 } })).not.toBe(draftKey(base));
    expect(
      draftKey({
        ...base,
        matrix: { ...base.matrix, models: [{ agent: 'mock', model: 'mock-a' }] },
      })
    ).not.toBe(draftKey(base));
    expect(
      draftKey({
        ...base,
        matrix: {
          ...base.matrix,
          prompts: [{ prompt_template_id: 'tpl-1' }, { prompt_template_id: null }],
        },
      })
    ).not.toBe(draftKey(base));
    expect(draftKey({ ...base, budget_usd: '50.00' })).not.toBe(draftKey(base));
    expect(draftKey({ ...base, push_branches: true })).not.toBe(draftKey(base));
    expect(
      draftKey({ ...base, verify: { image: 'python:3.12', command: 'pytest' } })
    ).not.toBe(draftKey(base));
  });

  it('draftKey ignores fields that cannot move a dollar', () => {
    const base = makeDraft();
    expect(draftKey({ ...base, name: 'renamed', description: 'notes' })).toBe(draftKey(base));
  });

  it('a dry run makes the launch gate fresh for that draft and only that draft', async () => {
    const draft = makeDraft();
    vi.mocked(experimentsApi.dryRun).mockResolvedValue(makeEstimate());

    expect(estimateIsFresh(get(experimentsStore), draft)).toBe(false);

    await experimentsStore.dryRun(draft);

    expect(estimateIsFresh(get(experimentsStore), draft)).toBe(true);
    const edited = { ...draft, matrix: { ...draft.matrix, repeat: 5 } };
    expect(estimateIsFresh(get(experimentsStore), edited)).toBe(false);
  });

  it('clearEstimate re-closes the gate (called on every matrix edit)', async () => {
    const draft = makeDraft();
    vi.mocked(experimentsApi.dryRun).mockResolvedValue(makeEstimate());
    await experimentsStore.dryRun(draft);

    experimentsStore.clearEstimate();

    expect(get(experimentsStore).estimate).toBeNull();
    expect(estimateIsFresh(get(experimentsStore), draft)).toBe(false);
  });

  it('a failed dry run leaves NO estimate behind, so the gate stays shut', async () => {
    vi.mocked(experimentsApi.dryRun).mockRejectedValue(new Error('models: unknown agent "gpt"'));

    const result = await experimentsStore.dryRun(makeDraft());

    expect(result).toBeNull();
    const state = get(experimentsStore);
    expect(state.estimate).toBeNull();
    expect(state.estimateKey).toBeNull();
    expect(state.estimateError).toContain('unknown agent');
  });

  it('createAndLaunch REFUSES an uncosted matrix even if a button let it through', async () => {
    const id = await experimentsStore.createAndLaunch(makeDraft());

    expect(id).toBeNull();
    expect(experimentsApi.create).not.toHaveBeenCalled();
    expect(experimentsApi.launch).not.toHaveBeenCalled();
    expect(get(experimentsStore).error).toContain('dry run');
  });

  it('createAndLaunch REFUSES when the draft was edited after the dry run', async () => {
    const draft = makeDraft();
    vi.mocked(experimentsApi.dryRun).mockResolvedValue(makeEstimate());
    await experimentsStore.dryRun(draft);

    const edited = { ...draft, matrix: { ...draft.matrix, repeat: 10 } };
    const id = await experimentsStore.createAndLaunch(edited);

    expect(id).toBeNull();
    expect(experimentsApi.create).not.toHaveBeenCalled();
  });

  it('createAndLaunch creates then launches once the matrix is costed', async () => {
    const draft = makeDraft();
    vi.mocked(experimentsApi.dryRun).mockResolvedValue(makeEstimate());
    vi.mocked(experimentsApi.create).mockResolvedValue(makeSummary({ status: 'draft' }));
    vi.mocked(experimentsApi.launch).mockResolvedValue(makeLaunchResponse());
    vi.mocked(experimentsApi.list).mockResolvedValue([makeSummary()]);
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail());

    await experimentsStore.dryRun(draft);
    const id = await experimentsStore.createAndLaunch(draft);

    expect(id).toBe('exp-1');
    expect(experimentsApi.create).toHaveBeenCalledWith(draft);
    expect(experimentsApi.launch).toHaveBeenCalledWith('exp-1');
    expect(get(experimentsStore).detail?.cells).toHaveLength(1);
  });

  it('launchSaved refuses a draft whose estimate is not the one on screen', async () => {
    vi.mocked(experimentsApi.estimate).mockResolvedValue(makeEstimate());
    await experimentsStore.estimateSaved('exp-9');

    expect(await experimentsStore.launchSaved('exp-OTHER')).toBe(false);
    expect(experimentsApi.launch).not.toHaveBeenCalled();

    vi.mocked(experimentsApi.launch).mockResolvedValue(makeLaunchResponse({ id: 'exp-9' }));
    vi.mocked(experimentsApi.list).mockResolvedValue([]);
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail({ id: 'exp-9' }));
    expect(await experimentsStore.launchSaved('exp-9')).toBe(true);
  });
});

// -----------------------------------------------------------------------------
// Formatting: no data must never look like a measured zero
// -----------------------------------------------------------------------------

describe('formatting refuses to invent zeros', () => {
  it('formatUsd renders an em dash for null, never $0.00', () => {
    expect(formatUsd(null)).toBe('—');
    expect(formatUsd(undefined)).toBe('—');
    expect(formatUsd('')).toBe('—');
    expect(formatUsd('0.000000')).toBe('$0.00');
  });

  it('formatUsd trims trailing zeros to at least two decimals without a float', () => {
    expect(formatUsd('7.440000')).toBe('$7.44');
    expect(formatUsd('1.234500')).toBe('$1.2345');
    expect(formatUsd('12')).toBe('$12.00');
    expect(formatUsd('-0.500000')).toBe('-$0.50');
    // A 20-digit amount survives intact: string in, string out.
    expect(formatUsd('12345678901234.567891')).toBe('$12345678901234.567891');
  });

  it('formatUsd shows an unparseable value rather than hiding it', () => {
    expect(formatUsd('n/a')).toBe('n/a');
  });

  it('formatRate renders N/A for null and 0% only for a real zero', () => {
    expect(formatRate(null)).toBe('N/A');
    expect(formatRate(undefined)).toBe('N/A');
    expect(formatRate(0)).toBe('0%');
    expect(formatRate(0.6666)).toBe('67%');
    expect(formatRate(1)).toBe('100%');
  });

  it('formatDuration renders an em dash for null', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(450)).toBe('450ms');
    expect(formatDuration(1500)).toBe('1.5s');
    expect(formatDuration(125000)).toBe('2m 5s');
  });
});

describe('cell arithmetic', () => {
  it('cellCount is models x prompts x repeat', () => {
    expect(cellCount(makeDraft())).toBe(6);
    expect(
      cellCount(
        makeDraft({
          matrix: {
            models: [{ agent: 'mock', model: null }],
            prompts: [{ prompt_template_id: null }, { prompt_template_id: 't' }],
            repeat: 4,
          },
        })
      )
    ).toBe(8);
  });

  it('cellsDone counts every terminal status, including the refused ones', () => {
    expect(
      cellsDone({
        cells_total: 10,
        by_status: { pending: 2, running: 1, passed: 3, failed: 2, error: 1, skipped_budget: 1 },
      })
    ).toBe(7);
  });
});

// -----------------------------------------------------------------------------
// List / detail plumbing
// -----------------------------------------------------------------------------

describe('experimentsStore.loadAll', () => {
  it('populates the list', async () => {
    vi.mocked(experimentsApi.list).mockResolvedValue([makeSummary(), makeSummary({ id: 'exp-2' })]);

    await experimentsStore.loadAll();

    expect(get(experimentsStore).list).toHaveLength(2);
    expect(get(experimentsStore).error).toBeNull();
  });

  it('surfaces a load failure instead of rendering an empty list as "no experiments"', async () => {
    vi.mocked(experimentsApi.list).mockRejectedValue(new Error('backend down'));

    await experimentsStore.loadAll();

    expect(get(experimentsStore).error).toBe('backend down');
    expect(get(experimentsStore).loading).toBe(false);
  });
});

describe('experimentsStore.select', () => {
  it('loads the detail and syncs the matching list row', async () => {
    vi.mocked(experimentsApi.list).mockResolvedValue([makeSummary({ status: 'running' })]);
    await experimentsStore.loadAll();

    vi.mocked(experimentsApi.get).mockResolvedValue(
      makeDetail({ status: 'complete', by_status: { passed: 6 } })
    );
    await experimentsStore.select('exp-1');

    const state = get(experimentsStore);
    expect(state.selectedId).toBe('exp-1');
    expect(state.detail?.cells).toHaveLength(1);
    expect(state.list[0].status).toBe('complete');
    // The list row is a summary: the detail's cells array must not leak into it.
    expect('cells' in state.list[0]).toBe(false);
  });

  it('drops the previous experiment views when a different one is selected', async () => {
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail());
    vi.mocked(experimentsApi.leaderboard).mockResolvedValue({
      experiment_id: 'exp-1',
      feature_id: null,
      ranked: false,
      note: 'note',
      variants: [],
      cost_coverage: null,
      warnings: [],
    } as Leaderboard);
    await experimentsStore.select('exp-1');
    await experimentsStore.loadLeaderboard('exp-1');
    expect(get(experimentsStore).leaderboard).not.toBeNull();

    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail({ id: 'exp-2' }));
    await experimentsStore.select('exp-2');

    expect(get(experimentsStore).leaderboard).toBeNull();
  });
});

// -----------------------------------------------------------------------------
// WS frames
// -----------------------------------------------------------------------------

describe('applyStatusFrame', () => {
  it('merges progress into the list row without clobbering the matrix', async () => {
    vi.mocked(experimentsApi.list).mockResolvedValue([makeSummary()]);
    await experimentsStore.loadAll();

    experimentsStore.applyStatusFrame({
      id: 'exp-1',
      name: 'opus vs haiku',
      status: 'complete',
      cells_total: 6,
      by_status: { passed: 5, failed: 1 },
      spend_usd: '1.18',
      budget_usd: '5.00',
      cost_coverage: 0.5,
      stalled: false,
    });

    const row = get(experimentsStore).list[0];
    expect(row.status).toBe('complete');
    expect(row.spend_usd).toBe('1.18');
    expect(row.cost_coverage).toBe(0.5);
    // Fields the frame does not carry survive.
    expect(row.matrix!.models).toHaveLength(2);
    expect(row.target_id).toBe('card-1');
  });

  it('reloads rather than fabricating a row for an unknown experiment', async () => {
    vi.mocked(experimentsApi.list).mockResolvedValue([]);
    await experimentsStore.loadAll();
    vi.mocked(experimentsApi.list).mockClear();

    experimentsStore.applyStatusFrame({
      id: 'exp-new',
      name: 'from another tab',
      status: 'running',
      cells_total: 2,
      by_status: { pending: 2 },
      spend_usd: '0.00',
      budget_usd: '1.00',
      cost_coverage: null,
      stalled: false,
    });

    expect(experimentsApi.list).toHaveBeenCalledTimes(1);
    expect(get(experimentsStore).list).toHaveLength(0);
  });
});

describe('applyCellFrame', () => {
  it('merges a cell transition and preserves the fields the frame omits', async () => {
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail());
    await experimentsStore.select('exp-1');

    experimentsStore.applyCellFrame({
      id: 'cell-1',
      experiment_id: 'exp-1',
      cell_index: 0,
      variant_index: 0,
      status: 'passed',
      pipeline_run_id: 'run-7',
      label: 'a / default',
      agent: 'mock',
      model: 'mock-a',
      prompt_template_id: null,
      prompt_version: null,
    });

    const cell = get(experimentsStore).detail!.cells[0];
    expect(cell.status).toBe('passed');
    expect(cell.pipeline_run_id).toBe('run-7');
    // started_at is not on the frame and must not be blanked by the merge.
    expect(cell.started_at).toBe('2026-08-30T00:00:02Z');
  });

  it('ignores frames for an experiment that is not on screen', async () => {
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail());
    await experimentsStore.select('exp-1');
    vi.mocked(experimentsApi.get).mockClear();

    experimentsStore.applyCellFrame({
      id: 'other-cell',
      experiment_id: 'exp-OTHER',
      cell_index: 0,
      variant_index: 0,
      status: 'passed',
      pipeline_run_id: null,
      label: null,
      agent: 'mock',
      model: null,
      prompt_template_id: null,
      prompt_version: null,
    });

    expect(experimentsApi.get).not.toHaveBeenCalled();
    expect(get(experimentsStore).detail!.cells[0].status).toBe('running');
  });

  it('refetches the matrix when a frame names a cell this client has not seen', async () => {
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail({ cells: [] }));
    await experimentsStore.select('exp-1');
    vi.mocked(experimentsApi.get).mockClear();
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail());

    experimentsStore.applyCellFrame({
      id: 'cell-1',
      experiment_id: 'exp-1',
      cell_index: 0,
      variant_index: 0,
      status: 'running',
      pipeline_run_id: null,
      label: 'a / default',
      agent: 'mock',
      model: 'mock-a',
      prompt_template_id: null,
      prompt_version: null,
    });

    expect(experimentsApi.get).toHaveBeenCalledWith('exp-1');
  });
});

describe('experimentsStore.abort / resume', () => {
  it('abort refetches so the cancelled cells come from the server, not a guess', async () => {
    vi.mocked(experimentsApi.abort).mockResolvedValue({
      id: 'exp-1',
      status: 'aborted',
      cancelled: 4,
      still_running: 1,
    });
    vi.mocked(experimentsApi.list).mockResolvedValue([makeSummary({ status: 'aborted' })]);
    vi.mocked(experimentsApi.get).mockResolvedValue(
      makeDetail({ status: 'aborted', cells: [makeCell({ status: 'cancelled' })] })
    );

    await experimentsStore.abort('exp-1');

    expect(get(experimentsStore).detail!.cells[0].status).toBe('cancelled');
  });

  it('resume returns how many cells it dispatched', async () => {
    vi.mocked(experimentsApi.resume).mockResolvedValue({
      id: 'exp-1',
      status: 'running',
      dispatched: 3,
      reset_dispatching: 0,
    });
    vi.mocked(experimentsApi.get).mockResolvedValue(makeDetail());

    expect(await experimentsStore.resume('exp-1')).toBe(3);
  });
});
