import { writable, derived } from 'svelte/store';
import type { Pipeline, PipelineCreate, PipelineUpdate, PipelineRun, PipelineRunCreate, RunStatus, StepRun } from '../api/types';
import { pipelines as pipelinesApi, pipelineRuns as runsApi } from '../api/client';

// Pipelines store
function createPipelinesStore() {
  const { subscribe, set, update } = writable<Pipeline[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },

    async load(repoId: string) {
      loading.set(true);
      error.set(null);
      try {
        const data = await pipelinesApi.listForRepo(repoId);
        set(data);
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load pipelines');
      } finally {
        loading.set(false);
      }
    },

    async create(repoId: string, data: PipelineCreate) {
      error.set(null);
      try {
        const pipeline = await pipelinesApi.create(repoId, data);
        update(pipelines => [...pipelines, pipeline]);
        return pipeline;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to create pipeline');
        throw e;
      }
    },

    async update(id: string, data: PipelineUpdate) {
      error.set(null);
      try {
        const pipeline = await pipelinesApi.update(id, data);
        update(pipelines => pipelines.map(p => p.id === id ? pipeline : p));
        return pipeline;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to update pipeline');
        throw e;
      }
    },

    async delete(id: string) {
      error.set(null);
      try {
        await pipelinesApi.delete(id);
        update(pipelines => pipelines.filter(p => p.id !== id));
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to delete pipeline');
        throw e;
      }
    },

    async run(id: string, data?: PipelineRunCreate) {
      error.set(null);
      try {
        const run = await pipelinesApi.run(id, data);
        // Add run to active runs store
        activeRunsStore.addRun(run);
        return run;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to start pipeline');
        throw e;
      }
    },

    updateLocal(pipeline: Pipeline) {
      update(pipelines => {
        // Only update existing pipelines, never add new ones
        // Adding is handled by create() to avoid race conditions with WebSocket
        const existing = pipelines.find(p => p.id === pipeline.id);
        if (existing) {
          return pipelines.map(p => p.id === pipeline.id ? pipeline : p);
        }
        return pipelines;
      });
    },

    deleteLocal(id: string) {
      update(pipelines => pipelines.filter(p => p.id !== id));
    },

    clear() {
      set([]);
    },
  };
}

export const pipelinesStore = createPipelinesStore();

// Active pipeline runs store
function createActiveRunsStore() {
  const { subscribe, set, update } = writable<Map<string, PipelineRun>>(new Map());
  const loading = writable(false);
  const error = writable<string | null>(null);

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },

    async loadRecent(limit: number = 20) {
      loading.set(true);
      error.set(null);
      try {
        const runs = await runsApi.list({ limit });
        update(map => {
          for (const run of runs) {
            map.set(run.id, run);
          }
          return new Map(map);
        });
        return runs;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load pipeline runs');
        throw e;
      } finally {
        loading.set(false);
      }
    },

    async loadForPipeline(pipelineId: string, limit: number = 10) {
      loading.set(true);
      error.set(null);
      try {
        const runs = await pipelinesApi.runs(pipelineId, limit);
        update(map => {
          for (const run of runs) {
            map.set(run.id, run);
          }
          return new Map(map);
        });
        return runs;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load pipeline runs');
        throw e;
      } finally {
        loading.set(false);
      }
    },

    async loadRun(runId: string) {
      error.set(null);
      try {
        const run = await runsApi.get(runId);
        update(map => {
          map.set(run.id, run);
          return new Map(map);
        });
        return run;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load pipeline run');
        throw e;
      }
    },

    async cancel(runId: string) {
      error.set(null);
      try {
        const run = await runsApi.cancel(runId);
        update(map => {
          map.set(run.id, run);
          return new Map(map);
        });
        return run;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to cancel pipeline run');
        throw e;
      }
    },

    addRun(run: PipelineRun) {
      update(map => {
        map.set(run.id, run);
        return new Map(map);
      });
    },

    updateRun(run: PipelineRun) {
      update(map => {
        // WS pipeline_run_status frames omit step_runs; a bare replace would
        // wipe step state the viewer is rendering. Merge, preserving the
        // previously-known step_runs when the incoming run lacks them.
        const prev = map.get(run.id);
        const step_runs = run.step_runs ?? prev?.step_runs ?? [];
        map.set(run.id, { ...prev, ...run, step_runs });
        return new Map(map);
      });
    },

    /**
     * Granular step frame (step_run_status / step_update WS messages).
     * Replaces the matching step_run in place, or inserts it (sorted by
     * step_index) when the local path creates StepRuns mid-run. Merges over
     * any existing entry because WS step dicts omit `logs`.
     */
    updateStepRun(stepRun: StepRun) {
      update(map => {
        const run = map.get(stepRun.pipeline_run_id);
        if (!run) return map;
        const steps = [...(run.step_runs ?? [])];
        const idx = steps.findIndex(s => s.step_index === stepRun.step_index);
        if (idx >= 0) {
          steps[idx] = { ...steps[idx], ...stepRun };
        } else {
          steps.push(stepRun);
          steps.sort((a, b) => a.step_index - b.step_index);
        }
        map.set(run.id, { ...run, step_runs: steps });
        return new Map(map);
      });
    },

    /**
     * step_update WS frame: a bare status transition for one step. Non-run
     * statuses from the local executor ("preparing", ...) are ignored; the
     * StepRun stays in its persisted state until a real transition arrives.
     */
    updateStepStatus(runId: string, stepIndex: number, status: string) {
      const valid: RunStatus[] = ['pending', 'running', 'passed', 'failed', 'cancelled'];
      if (!valid.includes(status as RunStatus)) return;
      update(map => {
        const run = map.get(runId);
        if (!run) return map;
        const steps = (run.step_runs ?? []).map(s =>
          s.step_index === stepIndex ? { ...s, status: status as RunStatus } : s
        );
        map.set(runId, { ...run, step_runs: steps });
        return new Map(map);
      });
    },

    removeRun(runId: string) {
      update(map => {
        map.delete(runId);
        return new Map(map);
      });
    },

    get(runId: string): PipelineRun | undefined {
      let run: PipelineRun | undefined;
      subscribe(map => {
        run = map.get(runId);
      })();
      return run;
    },

    clear() {
      set(new Map());
    },
  };
}

export const activeRunsStore = createActiveRunsStore();

// Derived store: runs grouped by status
export const runsByStatus = derived(activeRunsStore, ($runs) => {
  const grouped: Record<RunStatus, PipelineRun[]> = {
    pending: [],
    running: [],
    passed: [],
    failed: [],
    cancelled: [],
  };

  for (const run of $runs.values()) {
    grouped[run.status as RunStatus].push(run);
  }

  return grouped;
});

// -----------------------------------------------------------------------------
// Live step logs (Phase 12.2-INT)
//
// Log lines streamed over the WebSocket (step_log / step_log_batch frames)
// while a local step executes. Keyed "{runId}:{stepIndex}". This is a live
// tail, not the source of truth: the persisted StepRun.logs from the REST API
// remains authoritative once a step completes.
// -----------------------------------------------------------------------------

/** Bound on retained live lines per step so a chatty step can't grow memory unbounded. */
const MAX_LIVE_LOG_LINES = 2000;

export function stepLogKey(runId: string, stepIndex: number): string {
  return `${runId}:${stepIndex}`;
}

function createLiveStepLogsStore() {
  const { subscribe, set, update } = writable<Map<string, string[]>>(new Map());

  function appendLines(runId: string, stepIndex: number, lines: string[]) {
    if (lines.length === 0) return;
    update(map => {
      const key = stepLogKey(runId, stepIndex);
      const existing = map.get(key) ?? [];
      let next = [...existing, ...lines];
      if (next.length > MAX_LIVE_LOG_LINES) {
        next = next.slice(next.length - MAX_LIVE_LOG_LINES);
      }
      map.set(key, next);
      return new Map(map);
    });
  }

  return {
    subscribe,

    appendLines,

    appendLine(runId: string, stepIndex: number, line: string) {
      appendLines(runId, stepIndex, [line]);
    },

    clearRun(runId: string) {
      update(map => {
        let changed = false;
        for (const key of map.keys()) {
          if (key.startsWith(`${runId}:`)) {
            map.delete(key);
            changed = true;
          }
        }
        return changed ? new Map(map) : map;
      });
    },

    clear() {
      set(new Map());
    },
  };
}

export const liveStepLogsStore = createLiveStepLogsStore();

// Derived store: check if there are any active (pending/running) runs
export const hasActiveRuns = derived(activeRunsStore, ($runs) => {
  for (const run of $runs.values()) {
    if (run.status === 'pending' || run.status === 'running') {
      return true;
    }
  }
  return false;
});
