import { writable, derived } from 'svelte/store';
import type { Job, JobStatus } from '../api/types';
import { jobs as jobsApi } from '../api/client';

export interface JobStatusUpdate {
  id: string;
  card_id: string;
  status: JobStatus;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

function createJobsStore() {
  const { subscribe, set, update } = writable<Map<string, Job>>(new Map());
  const loading = writable(false);
  const error = writable<string | null>(null);

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },

    async load(jobId: string) {
      loading.set(true);
      error.set(null);
      try {
        const job = await jobsApi.get(jobId);
        update(jobs => {
          const newJobs = new Map(jobs);
          newJobs.set(job.id, job);
          return newJobs;
        });
        return job;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load job');
        throw e;
      } finally {
        loading.set(false);
      }
    },

    async cancel(jobId: string) {
      error.set(null);
      try {
        const job = await jobsApi.cancel(jobId);
        update(jobs => {
          const newJobs = new Map(jobs);
          newJobs.set(job.id, job);
          return newJobs;
        });
        return job;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to cancel job');
        throw e;
      }
    },

    updateFromWebSocket(data: JobStatusUpdate) {
      update(jobs => {
        const newJobs = new Map(jobs);
        const existing = newJobs.get(data.id);
        if (existing) {
          newJobs.set(data.id, {
            ...existing,
            status: data.status,
            error: data.error,
            started_at: data.started_at,
            completed_at: data.completed_at,
          });
        } else {
          // Create a minimal job entry for tracking
          newJobs.set(data.id, {
            id: data.id,
            card_id: data.card_id,
            runner_id: null,
            status: data.status,
            logs: '',
            error: data.error,
            started_at: data.started_at,
            completed_at: data.completed_at,
            created_at: new Date().toISOString(),
          });
        }
        return newJobs;
      });
    },

    get(jobId: string): Job | undefined {
      let job: Job | undefined;
      subscribe(jobs => {
        job = jobs.get(jobId);
      })();
      return job;
    },

    getByCardId(cardId: string): Job | undefined {
      let foundJob: Job | undefined;
      subscribe(jobs => {
        for (const job of jobs.values()) {
          if (job.card_id === cardId) {
            // Return the most recent job for this card
            if (!foundJob || job.created_at > foundJob.created_at) {
              foundJob = job;
            }
          }
        }
      })();
      return foundJob;
    },

    clear() {
      set(new Map());
    },
  };
}

export const jobsStore = createJobsStore();

// Derived store to get job by card ID
export function getJobForCard(cardId: string) {
  return derived(jobsStore, ($jobs) => {
    let latestJob: Job | undefined;
    for (const job of $jobs.values()) {
      if (job.card_id === cardId) {
        if (!latestJob || job.created_at > latestJob.created_at) {
          latestJob = job;
        }
      }
    }
    return latestJob;
  });
}

// Derived store to check if any jobs are currently running
export const hasRunningJobs = derived(jobsStore, ($jobs) => {
  for (const job of $jobs.values()) {
    if (job.status === 'running' || job.status === 'queued') {
      return true;
    }
  }
  return false;
});
