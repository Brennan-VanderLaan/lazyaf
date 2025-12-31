import { writable, derived } from 'svelte/store';
import type { Pipeline, PipelineCreate, PipelineUpdate, PipelineRun, PipelineRunCreate, RunStatus } from '../api/types';
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
        const existing = pipelines.find(p => p.id === pipeline.id);
        if (existing) {
          return pipelines.map(p => p.id === pipeline.id ? pipeline : p);
        } else {
          return [...pipelines, pipeline];
        }
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
        map.set(run.id, run);
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

// Derived store: check if there are any active (pending/running) runs
export const hasActiveRuns = derived(activeRunsStore, ($runs) => {
  for (const run of $runs.values()) {
    if (run.status === 'pending' || run.status === 'running') {
      return true;
    }
  }
  return false;
});
