import { writable, derived } from 'svelte/store';
import type { Job, JobStatus } from '../api/types';
import { jobs as jobsApi } from '../api/client';
import { timestampOrder } from '../utils/time';

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
          // A `job_status` frame for a job this tab has never loaded. The
          // frame carries only the six fields above, so the rest are spelled
          // out as "not known yet" rather than left off: the object was
          // previously typed as a `Job` while missing nine of its fields,
          // which svelte-check reported and which let `job.test_pass_count`
          // read `undefined` where the template tests it against `null`.
          // A later `load()` replaces this row with the real one.
          newJobs.set(data.id, {
            id: data.id,
            card_id: data.card_id,
            runner_id: null,
            runner_type: null,
            status: data.status,
            logs: '',
            error: data.error,
            started_at: data.started_at,
            completed_at: data.completed_at,
            created_at: new Date().toISOString(),
            step_type: 'agent',
            step_config: null,
            tests_run: false,
            tests_passed: null,
            test_pass_count: null,
            test_fail_count: null,
            test_skip_count: null,
            test_output: null,
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
            // Compared as INSTANTS, not as strings. This store mixes two
            // spellings of the same moment - `created_at` from the API and
            // the `new Date().toISOString()` stamped above for a job first
            // seen over the socket - and ">" on strings only accidentally
            // agrees with ">" on times while both sides happen to be spelled
            // the same way.
            if (!foundJob || timestampOrder(job.created_at) > timestampOrder(foundJob.created_at)) {
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
        if (!latestJob || timestampOrder(job.created_at) > timestampOrder(latestJob.created_at)) {
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
