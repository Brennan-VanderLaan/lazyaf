/**
 * Experiments store (Phase 12.6.5).
 *
 * Holds the experiment list, the selected experiment's cells, its leaderboard,
 * and — the part that matters — the DRY-RUN ESTIMATE that gates launching.
 *
 * Three rules this store exists to enforce, all of them R1 ("nothing dark"):
 *
 *  1. LAUNCH IS GATED ON A FRESH ESTIMATE. `estimate` is stored together with
 *     `estimateKey`, a pure hash of the draft it was computed from. Edit any
 *     cost-bearing field of the draft and the key stops matching, so the
 *     Launch button re-disables until the matrix is re-costed. The gate is
 *     here, not in the component, so it is unit-tested rather than eyeballed.
 *
 *  2. MONEY NEVER BECOMES A FLOAT. Dollars arrive as decimal strings and are
 *     formatted as strings (`formatUsd`). Nothing in this file calls
 *     parseFloat on a dollar amount.
 *
 *  3. "NO DATA" NEVER RENDERS AS ZERO. `formatRate(null)` is "N/A", not "0%";
 *     `formatUsd(null)` is an em dash, not "$0.00". A missing measurement and
 *     a measured zero are different facts.
 *
 * Live updates: the two WS frames are applied through `applyStatusFrame` /
 * `applyCellFrame`, which MERGE (the frames are subsets of the REST rows — see
 * the contract note in api/types.ts). A frame for a row this store has never
 * seen triggers a reload rather than fabricating the missing fields.
 */
import { writable, get } from 'svelte/store';
import type {
  ExperimentSummary,
  ExperimentDetail,
  ExperimentCreate,
  ExperimentCell,
  ExperimentEstimate,
  ExperimentStatusFrame,
  ExperimentCellFrame,
  Leaderboard,
} from '../api/types';
import { experiments as experimentsApi } from '../api/client';

export interface ExperimentsState {
  list: ExperimentSummary[];
  loading: boolean;
  error: string | null;

  selectedId: string | null;
  detail: ExperimentDetail | null;
  detailLoading: boolean;

  results: ExperimentCell[] | null;
  resultsLoading: boolean;

  leaderboard: Leaderboard | null;
  leaderboardLoading: boolean;

  /** The last dry run, and the draft it priced. Both, or neither. */
  estimate: ExperimentEstimate | null;
  estimateKey: string | null;
  estimateLoading: boolean;
  estimateError: string | null;

  launching: boolean;
}

function initialState(): ExperimentsState {
  return {
    list: [],
    loading: false,
    error: null,
    selectedId: null,
    detail: null,
    detailLoading: false,
    results: null,
    resultsLoading: false,
    leaderboard: null,
    leaderboardLoading: false,
    estimate: null,
    estimateKey: null,
    estimateLoading: false,
    estimateError: null,
    launching: false,
  };
}

function message(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback;
}

// -----------------------------------------------------------------------------
// Pure helpers (exported: the components render through them and the unit tests
// pin them without a store)
// -----------------------------------------------------------------------------

/**
 * Identity of everything about a draft that can change what it costs. An
 * estimate is only valid for the draft that produced this exact key.
 *
 * Deliberately INCLUDES the budget: the estimate response carries
 * `within_budget`, so a raised cap changes the answer the user was shown.
 * Deliberately EXCLUDES name/description, which cannot move a dollar.
 */
export function draftKey(draft: ExperimentCreate): string {
  return JSON.stringify({
    target_type: draft.target_type,
    target_id: draft.target_id,
    repo_id: draft.repo_id ?? '',
    models: draft.matrix.models.map(m => [m.agent, m.model, m.step_config ?? null]),
    prompts: draft.matrix.prompts.map(p => [p.prompt_template_id, p.step_config ?? null]),
    repeat: draft.matrix.repeat,
    budget_usd: draft.budget_usd,
    verify: draft.verify ?? null,
    push_branches: draft.push_branches ?? false,
  });
}

/** True when the stored estimate was computed from exactly this draft. */
export function estimateIsFresh(state: ExperimentsState, draft: ExperimentCreate): boolean {
  return state.estimate !== null && state.estimateKey === draftKey(draft);
}

/** cells = models x prompts x repeat. Shown live while the matrix is edited. */
export function cellCount(draft: ExperimentCreate): number {
  const { models, prompts, repeat } = draft.matrix;
  return models.length * prompts.length * Math.max(0, repeat);
}

/**
 * Format a decimal money string for display WITHOUT going through a float.
 * `null` renders as an em dash — an unknown cost is not a zero cost.
 */
export function formatUsd(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—';
  const match = /^(-?)(\d+)(?:\.(\d*))?$/.exec(value.trim());
  if (!match) return value; // Unexpected shape: show it rather than hide it.
  const [, sign, whole, rawFraction = ''] = match;
  let fraction = rawFraction.replace(/0+$/, '');
  while (fraction.length < 2) fraction += '0';
  return `${sign}$${whole}.${fraction}`;
}

/** A rate in [0,1] as a percentage. `null` is "N/A" — never "0%". */
export function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'N/A';
  return `${Math.round(value * 100)}%`;
}

/** Milliseconds as a short human duration. `null` is an em dash. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

/** Cells that reached a terminal state, over the total. */
export function cellsDone(progress: {
  by_status: Partial<Record<string, number>>;
  cells_total: number;
}): number {
  const terminal = ['passed', 'failed', 'error', 'cancelled', 'skipped_budget'];
  return terminal.reduce((sum, s) => sum + (progress.by_status[s] ?? 0), 0);
}

// -----------------------------------------------------------------------------
// Store
// -----------------------------------------------------------------------------

function createExperimentsStore() {
  const { subscribe, set, update } = writable<ExperimentsState>(initialState());

  async function loadAll(filters?: { status?: string; target_id?: string; repo_id?: string }) {
    update(s => ({ ...s, loading: true, error: null }));
    try {
      const list = await experimentsApi.list(filters);
      update(s => ({ ...s, list, loading: false }));
    } catch (e) {
      update(s => ({ ...s, loading: false, error: message(e, 'Failed to load experiments') }));
    }
  }

  async function loadDetail(id: string) {
    update(s => ({ ...s, detailLoading: true, error: null }));
    try {
      const detail = await experimentsApi.get(id);
      update(s => ({
        ...s,
        detail,
        detailLoading: false,
        // Keep the list row in step with the detail we just fetched.
        list: s.list.map(row => (row.id === id ? { ...row, ...stripCells(detail) } : row)),
      }));
    } catch (e) {
      update(s => ({
        ...s,
        detailLoading: false,
        error: message(e, 'Failed to load experiment'),
      }));
    }
  }

  function stripCells(detail: ExperimentDetail): ExperimentSummary {
    const { cells: _cells, ...summary } = detail;
    return summary;
  }

  return {
    subscribe,

    loadAll,
    loadDetail,

    /** Select an experiment and load its cells + leaderboard. */
    async select(id: string) {
      update(s => ({
        ...s,
        selectedId: id,
        // Clear the previous experiment's derived views rather than showing
        // one experiment's numbers under another's name.
        detail: s.detail?.id === id ? s.detail : null,
        results: null,
        leaderboard: null,
      }));
      await loadDetail(id);
    },

    clearSelection() {
      update(s => ({
        ...s,
        selectedId: null,
        detail: null,
        results: null,
        leaderboard: null,
      }));
    },

    async loadResults(id: string) {
      update(s => ({ ...s, resultsLoading: true }));
      try {
        const results = await experimentsApi.results(id);
        update(s => ({ ...s, results, resultsLoading: false }));
      } catch (e) {
        update(s => ({
          ...s,
          resultsLoading: false,
          error: message(e, 'Failed to load results'),
        }));
      }
    },

    async loadLeaderboard(id: string) {
      update(s => ({ ...s, leaderboardLoading: true }));
      try {
        const leaderboard = await experimentsApi.leaderboard(id);
        update(s => ({ ...s, leaderboard, leaderboardLoading: false }));
      } catch (e) {
        update(s => ({
          ...s,
          leaderboardLoading: false,
          error: message(e, 'Failed to load leaderboard'),
        }));
      }
    },

    /**
     * Cost the matrix. Creates NOTHING. On success the estimate is stored with
     * the key of the draft that produced it, which is what unlocks Launch.
     */
    async dryRun(draft: ExperimentCreate): Promise<ExperimentEstimate | null> {
      update(s => ({ ...s, estimateLoading: true, estimateError: null }));
      try {
        const estimate = await experimentsApi.dryRun(draft);
        update(s => ({
          ...s,
          estimate,
          estimateKey: draftKey(draft),
          estimateLoading: false,
        }));
        return estimate;
      } catch (e) {
        update(s => ({
          ...s,
          estimate: null,
          estimateKey: null,
          estimateLoading: false,
          estimateError: message(e, 'Dry run failed'),
        }));
        return null;
      }
    },

    /** Drop the estimate so Launch re-gates (called on every matrix edit). */
    clearEstimate() {
      update(s => ({ ...s, estimate: null, estimateKey: null, estimateError: null }));
    },

    /**
     * Create the draft and launch it. REFUSES unless a fresh dry run for this
     * exact draft is in hand — the guardrail is enforced here, not only by a
     * disabled button, so no code path can spend money uncosted.
     */
    async createAndLaunch(draft: ExperimentCreate): Promise<string | null> {
      const state = get({ subscribe });
      if (!estimateIsFresh(state, draft)) {
        update(s => ({
          ...s,
          error:
            'Run the dry run for this exact matrix before launching — the estimate on ' +
            'screen was computed for a different one.',
        }));
        return null;
      }
      update(s => ({ ...s, launching: true, error: null }));
      try {
        const created = await experimentsApi.create(draft);
        // The launch body is deliberately ignored: the detail refetch below is
        // the single source of what actually happened.
        await experimentsApi.launch(created.id);
        update(s => ({ ...s, launching: false, selectedId: created.id }));
        await loadAll();
        await loadDetail(created.id);
        return created.id;
      } catch (e) {
        update(s => ({ ...s, launching: false, error: message(e, 'Launch failed') }));
        return null;
      }
    },

    /** Estimate an already-saved draft (GET /estimate), keyed by its id. */
    async estimateSaved(id: string): Promise<ExperimentEstimate | null> {
      update(s => ({ ...s, estimateLoading: true, estimateError: null }));
      try {
        const estimate = await experimentsApi.estimate(id);
        update(s => ({
          ...s,
          estimate,
          estimateKey: `saved:${id}`,
          estimateLoading: false,
        }));
        return estimate;
      } catch (e) {
        update(s => ({
          ...s,
          estimate: null,
          estimateKey: null,
          estimateLoading: false,
          estimateError: message(e, 'Estimate failed'),
        }));
        return null;
      }
    },

    /** Launch a saved draft. Same gate: its estimate must be on screen. */
    async launchSaved(id: string): Promise<boolean> {
      const state = get({ subscribe });
      if (state.estimate === null || state.estimateKey !== `saved:${id}`) {
        update(s => ({
          ...s,
          error: 'Fetch the estimate for this draft before launching it.',
        }));
        return false;
      }
      update(s => ({ ...s, launching: true, error: null }));
      try {
        await experimentsApi.launch(id);
        update(s => ({ ...s, launching: false }));
        await loadAll();
        await loadDetail(id);
        return true;
      } catch (e) {
        update(s => ({ ...s, launching: false, error: message(e, 'Launch failed') }));
        return false;
      }
    },

    /** Cancel pending cells. Running cells finish and still count. */
    async abort(id: string) {
      try {
        await experimentsApi.abort(id);
        await loadAll();
        await loadDetail(id);
      } catch (e) {
        update(s => ({ ...s, error: message(e, 'Abort failed') }));
      }
    },

    /** Re-pump a stalled experiment. Returns how many cells were dispatched. */
    async resume(id: string): Promise<number | null> {
      try {
        const result = await experimentsApi.resume(id);
        await loadDetail(id);
        return result.dispatched;
      } catch (e) {
        update(s => ({ ...s, error: message(e, 'Resume failed') }));
        return null;
      }
    },

    async remove(id: string) {
      try {
        await experimentsApi.delete(id);
        update(s => ({
          ...s,
          list: s.list.filter(row => row.id !== id),
          selectedId: s.selectedId === id ? null : s.selectedId,
          detail: s.detail?.id === id ? null : s.detail,
        }));
      } catch (e) {
        update(s => ({ ...s, error: message(e, 'Delete failed') }));
      }
    },

    // -------------------------------------------------------------------------
    // WS frame application (wired from stores/websocket.ts by the integrator)
    // -------------------------------------------------------------------------

    /**
     * `experiment_status`: a progress delta. Merged into the list row and, when
     * it is the selected experiment, into the detail — never replacing either,
     * because the frame carries no matrix, no verify block and no cells.
     */
    applyStatusFrame(frame: ExperimentStatusFrame) {
      let known = false;
      update(s => {
        const list = s.list.map(row => {
          if (row.id !== frame.id) return row;
          known = true;
          return { ...row, ...frame };
        });
        const detail =
          s.detail && s.detail.id === frame.id ? { ...s.detail, ...frame } : s.detail;
        return { ...s, list, detail };
      });
      // A frame for an experiment this client has never listed (created in
      // another tab) is a reload, not a fabricated row.
      if (!known && !get({ subscribe }).loading) {
        void loadAll();
      }
    },

    /**
     * `experiment_cell_status`: one cell's transition. Merged into the loaded
     * detail; frames for other experiments are ignored (the board only ever
     * shows one matrix at a time).
     */
    applyCellFrame(frame: ExperimentCellFrame) {
      const state = get({ subscribe });
      if (!state.detail || state.detail.id !== frame.experiment_id) return;
      const index = state.detail.cells.findIndex(cell => cell.id === frame.id);
      if (index === -1) {
        // Cells this client has not seen (launch raced the detail fetch): pull
        // the real rows rather than inventing the fields the frame omits.
        if (!state.detailLoading) void loadDetail(frame.experiment_id);
        return;
      }
      update(s => {
        if (!s.detail) return s;
        const cells = s.detail.cells.map(cell =>
          cell.id === frame.id ? { ...cell, ...frame } : cell
        );
        return { ...s, detail: { ...s.detail, cells } };
      });
    },

    clear() {
      set(initialState());
    },
  };
}

export const experimentsStore = createExperimentsStore();
