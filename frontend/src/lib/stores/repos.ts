import { writable, derived } from 'svelte/store';
import type { Repo, RepoCreate } from '../api/types';
import { repos as reposApi } from '../api/client';

function createReposStore() {
  const { subscribe, set, update } = writable<Repo[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },

    async load() {
      loading.set(true);
      error.set(null);
      try {
        const data = await reposApi.list();
        set(data);
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load repos');
      } finally {
        loading.set(false);
      }
    },

    async create(data: RepoCreate) {
      error.set(null);
      try {
        const repo = await reposApi.create(data);
        update(repos => [...repos, repo]);
        return repo;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to create repo');
        throw e;
      }
    },

    async ingest(data: RepoCreate) {
      error.set(null);
      try {
        const result = await reposApi.ingest(data);
        // Fetch the full repo object after ingest
        const repo = await reposApi.get(result.id);
        update(repos => [...repos, repo]);
        return repo;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to ingest repo');
        throw e;
      }
    },

    async delete(id: string) {
      error.set(null);
      try {
        await reposApi.delete(id);
        update(repos => repos.filter(r => r.id !== id));
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to delete repo');
        throw e;
      }
    },
  };
}

export const reposStore = createReposStore();

// Selected repo
export const selectedRepoId = writable<string | null>(null);

export const selectedRepo = derived(
  [reposStore, selectedRepoId],
  ([$repos, $selectedRepoId]) => $repos.find(r => r.id === $selectedRepoId) ?? null
);
