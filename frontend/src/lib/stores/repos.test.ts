import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import type { BranchInfo, Repo } from '../api/types';

vi.mock('../api/client', () => ({
  repos: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    ingest: vi.fn(),
    delete: vi.fn(),
    branches: vi.fn(),
  },
}));

import { branchesStore, reposStore } from './repos';
import { repos as reposApi } from '../api/client';

function makeRepo(overrides: Partial<Repo> = {}): Repo {
  return {
    id: 'repo-1',
    name: 'demo-repo',
    remote_url: null,
    default_branch: 'main',
    is_ingested: true,
    internal_git_url: '/git/repo-1.git',
    created_at: '2026-01-01T00:00:00+00:00',
    ...overrides,
  } as Repo;
}

async function seed(repos: Repo[]) {
  vi.mocked(reposApi.list).mockResolvedValueOnce(repos);
  await reposStore.load();
}

beforeEach(() => {
  vi.clearAllMocks();
});

/**
 * The bug these pin, seen in a browser against a live backend: clicking
 * "Create Repo" ONCE put demo-repo in the sidebar TWICE. The backend had one
 * row; a reload showed one. On a local backend the `repo_created` frame beats
 * the POST response, `updateLocal` appends it, and then `create` appended the
 * same repo again because it did not check for an existing id.
 */
describe('reposStore insert-or-replace by id', () => {
  it('does not duplicate when the WS frame beats the create response', async () => {
    await seed([]);
    const repo = makeRepo({ name: 'from ws' });

    reposStore.updateLocal(repo);
    vi.mocked(reposApi.create).mockResolvedValueOnce({ ...repo, name: 'from http' });
    await reposStore.create({ name: 'demo-repo' } as never);

    const repos = get(reposStore);
    expect(repos).toHaveLength(1);
    expect(repos[0].name).toBe('from http');
  });

  it('does not duplicate when the WS frame beats the ingest response', async () => {
    await seed([]);
    const repo = makeRepo({ id: 'repo-9', name: 'from ws' });

    reposStore.updateLocal(repo);
    vi.mocked(reposApi.ingest).mockResolvedValueOnce({ id: 'repo-9' } as never);
    vi.mocked(reposApi.get).mockResolvedValueOnce({ ...repo, name: 'from http' });
    await reposStore.ingest({ name: 'demo-repo' } as never);

    expect(get(reposStore)).toHaveLength(1);
    expect(get(reposStore)[0].name).toBe('from http');
  });

  it('still adopts a repo created by someone else', async () => {
    await seed([makeRepo({ id: 'repo-1' })]);

    reposStore.updateLocal(makeRepo({ id: 'repo-2', name: 'theirs' }));

    expect(get(reposStore).map(r => r.id)).toEqual(['repo-1', 'repo-2']);
  });

  it('replaces an existing repo in place rather than appending', async () => {
    await seed([makeRepo({ id: 'repo-1', default_branch: 'main' })]);

    reposStore.updateLocal(makeRepo({ id: 'repo-1', default_branch: 'trunk' }));

    const repos = get(reposStore);
    expect(repos).toHaveLength(1);
    expect(repos[0].default_branch).toBe('trunk');
  });

  it('deleteLocal removes only the targeted repo', async () => {
    await seed([makeRepo({ id: 'repo-1' }), makeRepo({ id: 'repo-2' })]);

    reposStore.deleteLocal('repo-1');

    expect(get(reposStore).map(r => r.id)).toEqual(['repo-2']);
  });
});


/**
 * LANE 3 - the branch list the sidebar renders.
 *
 * Branches are not part of the Repo row: a push moves refs and changes no row
 * at all. So the panel is snapshot-then-delta, and these pin the two failures
 * that made "I need to be able to see which branches are available" a bug
 * report - a delta that never arrives, and a failed fetch that renders as an
 * empty repo.
 */
describe('branchesStore', () => {
  const branch = (name: string, extra: Partial<BranchInfo> = {}): BranchInfo => ({
    name,
    commit: `sha-${name}`,
    is_default: false,
    is_lazyaf: name.startsWith('lazyaf/'),
    ...extra,
  });

  const listing = (repoId: string, branches: BranchInfo[]) => ({
    repo_id: repoId,
    branches,
    default_branch: branches.find(b => b.is_default)?.name ?? null,
    total: branches.length,
  });

  beforeEach(() => {
    branchesStore.clear();
  });

  it('loads the listing for a repo', async () => {
    vi.mocked(reposApi.branches).mockResolvedValueOnce(
      listing('repo-1', [branch('main', { is_default: true })]) as never,
    );

    await branchesStore.load('repo-1');

    const state = get(branchesStore);
    expect(state.repoId).toBe('repo-1');
    expect(state.branches.map(b => b.name)).toEqual(['main']);
    expect(state.defaultBranch).toBe('main');
    expect(state.error).toBeNull();
    expect(state.loaded).toBe(true);
  });

  it('a refs frame adds a pushed branch without any refetch', async () => {
    // THE reported defect. The push happens outside the browser, changes no
    // repo row, and before this frame existed the panel went on rendering the
    // list it had fetched before the push - "No branches yet. Push your repo
    // to get started." - until someone pressed F5.
    vi.mocked(reposApi.branches).mockResolvedValueOnce(listing('repo-1', []) as never);
    await branchesStore.load('repo-1');
    expect(get(branchesStore).branches).toEqual([]);

    branchesStore.applyRefsFrame(
      listing('repo-1', [branch('main', { is_default: true }), branch('lazyaf/ab12cd34')]),
    );

    expect(get(branchesStore).branches.map(b => b.name)).toEqual(['main', 'lazyaf/ab12cd34']);
    expect(vi.mocked(reposApi.branches)).toHaveBeenCalledTimes(1);
  });

  it('ignores a frame for a repo the user is not looking at', async () => {
    // Frames are broadcast for every repo on the backend. Adopting one for
    // another repo would label its branches with this repo's name.
    vi.mocked(reposApi.branches).mockResolvedValueOnce(
      listing('repo-1', [branch('main', { is_default: true })]) as never,
    );
    await branchesStore.load('repo-1');

    branchesStore.applyRefsFrame(listing('repo-2', [branch('someone-elses')]));

    expect(get(branchesStore).branches.map(b => b.name)).toEqual(['main']);
  });

  it('reports a failed listing as an error, never as an empty repo', async () => {
    // R1. Rendered as "no branches", a 500 told the user to push a repo they
    // had already pushed - the remedy for a state they were not in.
    vi.mocked(reposApi.branches).mockRejectedValueOnce(new Error('500: git storage unreadable'));

    await branchesStore.load('repo-1');

    const state = get(branchesStore);
    expect(state.error).toContain('git storage unreadable');
    expect(state.branches).toEqual([]);
    expect(state.loading).toBe(false);
    expect(state.loaded).toBe(true);
  });

  it('a later frame clears a stale error', async () => {
    vi.mocked(reposApi.branches).mockRejectedValueOnce(new Error('backend restarting'));
    await branchesStore.load('repo-1');
    expect(get(branchesStore).error).not.toBeNull();

    branchesStore.applyRefsFrame(listing('repo-1', [branch('main', { is_default: true })]));

    expect(get(branchesStore).error).toBeNull();
    expect(get(branchesStore).branches.map(b => b.name)).toEqual(['main']);
  });

  it('drops a response for a repo the user has already navigated away from', async () => {
    // Out-of-order replies must not resurrect an old repo's branches under a
    // new repo's name.
    let resolveSlow: (value: unknown) => void = () => {};
    vi.mocked(reposApi.branches).mockImplementationOnce(
      () => new Promise(resolve => { resolveSlow = resolve; }) as never,
    );
    const slow = branchesStore.load('repo-1');

    vi.mocked(reposApi.branches).mockResolvedValueOnce(
      listing('repo-2', [branch('trunk', { is_default: true })]) as never,
    );
    await branchesStore.load('repo-2');

    resolveSlow(listing('repo-1', [branch('stale')]));
    await slow;

    const state = get(branchesStore);
    expect(state.repoId).toBe('repo-2');
    expect(state.branches.map(b => b.name)).toEqual(['trunk']);
  });

  it('keeps the current list visible while refreshing the SAME repo', async () => {
    // The panel reloads on every repo_updated frame. Blanking to "Loading..."
    // each time would make the list flicker for no reason.
    vi.mocked(reposApi.branches).mockResolvedValueOnce(
      listing('repo-1', [branch('main', { is_default: true })]) as never,
    );
    await branchesStore.load('repo-1');

    let resolveRefresh: (value: unknown) => void = () => {};
    vi.mocked(reposApi.branches).mockImplementationOnce(
      () => new Promise(resolve => { resolveRefresh = resolve; }) as never,
    );
    const refreshing = branchesStore.load('repo-1');

    expect(get(branchesStore).loading).toBe(false);
    expect(get(branchesStore).branches.map(b => b.name)).toEqual(['main']);

    resolveRefresh(listing('repo-1', [branch('main', { is_default: true }), branch('lazyaf/new')]));
    await refreshing;

    expect(get(branchesStore).branches).toHaveLength(2);
  });
});
