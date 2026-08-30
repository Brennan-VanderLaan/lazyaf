import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import type { Repo } from '../api/types';

vi.mock('../api/client', () => ({
  repos: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    ingest: vi.fn(),
    delete: vi.fn(),
  },
}));

import { reposStore } from './repos';
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
