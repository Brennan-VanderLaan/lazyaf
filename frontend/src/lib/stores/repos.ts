import { writable, derived } from 'svelte/store';
import type { Repo, RepoCreate } from '../api/types';
import { repos as reposApi } from '../api/client';

function createReposStore() {
  const { subscribe, set, update } = writable<Repo[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);

  /**
   * Insert-or-replace by id. THE only way a repo enters this list.
   *
   * `create`/`ingest` used to append their own HTTP response unconditionally,
   * which races the `repo_created` frame the same write broadcasts: the frame
   * arrives first on a local backend, `updateLocal` appends it, and then the
   * POST resolves and appends the SAME repo a second time. One click, two
   * identical rows in the sidebar - the first thing a demo does, and it
   * looked like the backend had created two repositories (it had not; a
   * reload showed one). Both paths now go through this.
   */
  function upsert(repo: Repo) {
    update(repos => {
      const index = repos.findIndex(r => r.id === repo.id);
      if (index < 0) return [...repos, repo];
      const updated = [...repos];
      updated[index] = repo;
      return updated;
    });
  }

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
        upsert(repo);
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
        upsert(repo);
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

    // WebSocket updates - optimistic updates from other clients
    updateLocal(repo: Repo) {
      upsert(repo);
    },

    deleteLocal(id: string) {
      update(repos => repos.filter(r => r.id !== id));
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
