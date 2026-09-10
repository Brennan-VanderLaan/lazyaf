import { writable, derived, get } from 'svelte/store';
import type { BranchInfo, Repo, RepoCreate } from '../api/types';
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

// -----------------------------------------------------------------------------
// Branches of the selected repo.
//
// Branches are NOT part of the Repo row - they live in the refs of the internal
// git repo, and a push changes them without changing any row. So they get their
// own store, snapshot-then-delta like runners and endpoints: `load()` fetches
// `GET /api/repos/{id}/branches`, and the `repo_refs_changed` websocket frame
// (broadcast by every backend path that writes a ref: push, branch delete,
// orphan cleanup, reinitialize, sync) replaces the list in place.
//
// The frame carries the SAME body the fetch returns, so the two paths cannot
// produce different lists (R3, and the backend half of it is
// `routers.repos.build_branch_listing`).
//
// `error` is a first-class state, never folded into "no branches". A failed
// fetch rendered as an empty list is the exact defect this store replaces: the
// panel told the user to push a repo they had already pushed (R1).
// -----------------------------------------------------------------------------

/** `repo_refs_changed` payload: a full branch listing, tagged with its repo. */
export interface RepoRefsChangedFrame {
  repo_id: string;
  branches: BranchInfo[];
  default_branch: string | null;
  total: number;
}

export interface BranchesState {
  /** Which repo the list below belongs to. null before the first load. */
  repoId: string | null;
  branches: BranchInfo[];
  defaultBranch: string | null;
  /** True only while the FIRST load for a repo is in flight - see load(). */
  loading: boolean;
  /** Set when the listing could not be read. Never conflated with "empty". */
  error: string | null;
  /** False until a load for `repoId` has actually resolved. */
  loaded: boolean;
}

const EMPTY_BRANCHES: BranchesState = {
  repoId: null,
  branches: [],
  defaultBranch: null,
  loading: false,
  error: null,
  loaded: false,
};

function createBranchesStore() {
  const store = writable<BranchesState>({ ...EMPTY_BRANCHES });
  const { subscribe, set, update } = store;

  return {
    subscribe,

    /**
     * Fetch the branch listing for one repo.
     *
     * Re-entrant on purpose: the panel calls this whenever the selected repo
     * object changes identity, which includes the `repos.load()` a socket
     * reconnect performs - that is what re-snapshots branches after a gap in
     * which a `repo_refs_changed` frame was missed.
     *
     * `loading` is set only when there is nothing to show yet. A refresh of the
     * repo already on screen keeps its list visible until the new one arrives,
     * so a routine repo_updated frame cannot flicker the list to "Loading...".
     *
     * Responses for a repo the user has since navigated away from are dropped,
     * not rendered - out-of-order replies must not resurrect an old repo's
     * branches under a new repo's name.
     */
    async load(repoId: string) {
      const isNewRepo = get(store).repoId !== repoId;
      update(state => ({
        ...state,
        repoId,
        loading: isNewRepo ? true : state.loading,
        error: isNewRepo ? null : state.error,
        branches: isNewRepo ? [] : state.branches,
        defaultBranch: isNewRepo ? null : state.defaultBranch,
        loaded: isNewRepo ? false : state.loaded,
      }));

      try {
        const response = await reposApi.branches(repoId);
        update(state =>
          state.repoId !== repoId
            ? state
            : {
                repoId,
                branches: response.branches,
                defaultBranch: response.default_branch,
                loading: false,
                error: null,
                loaded: true,
              },
        );
      } catch (e) {
        const message = e instanceof Error ? e.message : 'Failed to load branches';
        update(state =>
          state.repoId !== repoId
            ? state
            : { ...state, loading: false, error: message, loaded: true },
        );
      }
    },

    /**
     * Apply a `repo_refs_changed` frame.
     *
     * Ignored unless it names the repo currently on screen: frames arrive for
     * every repo on the backend, and adopting one for a repo the user is not
     * looking at would label another repository's branches with this one's name.
     *
     * A frame also CLEARS a previous error - it is proof the backend can read
     * the refs right now.
     */
    applyRefsFrame(frame: RepoRefsChangedFrame) {
      update(state =>
        state.repoId !== frame.repo_id
          ? state
          : {
              repoId: frame.repo_id,
              branches: frame.branches,
              defaultBranch: frame.default_branch,
              loading: false,
              error: null,
              loaded: true,
            },
      );
    },

    /** Drop everything - used when no repo is selected. */
    clear() {
      set({ ...EMPTY_BRANCHES });
    },
  };
}

export const branchesStore = createBranchesStore();
