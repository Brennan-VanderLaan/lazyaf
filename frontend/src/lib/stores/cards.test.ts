import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import type { Card, CardStatus } from '../api/types';

// The stores call the API client at action time; mock it so load() can seed
// the singleton store without a network.
vi.mock('../api/client', () => ({
  cards: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    start: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
    retry: vi.fn(),
    rebase: vi.fn(),
    resolveConflicts: vi.fn(),
    resolveRebaseConflicts: vi.fn(),
  },
  repos: {
    list: vi.fn(),
  },
}));

import { cardsStore, cardsByStatus } from './cards';
import { cards as cardsApi } from '../api/client';

function makeCard(overrides: Partial<Card> = {}): Card {
  return {
    id: 'card-1',
    repo_id: 'repo-1',
    title: 'A card',
    description: '',
    status: 'todo',
    runner_type: 'any',
    step_type: 'agent',
    step_config: null,
    prompt_template: null,
    agent_file_ids: null,
    branch_name: null,
    pr_url: null,
    job_id: null,
    completed_runner_type: null,
    pipeline_run_id: null,
    pipeline_step_index: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

async function seed(cards: Card[]) {
  vi.mocked(cardsApi.list).mockResolvedValueOnce(cards);
  await cardsStore.load('repo-1');
}

beforeEach(() => {
  cardsStore.clear();
  vi.clearAllMocks();
});

describe('cardsStore.updateLocal', () => {
  it('replaces an existing card in place', async () => {
    await seed([makeCard({ id: 'c1', title: 'old' }), makeCard({ id: 'c2' })]);

    cardsStore.updateLocal(makeCard({ id: 'c1', title: 'new', status: 'in_progress' }));

    const cards = get(cardsStore);
    expect(cards).toHaveLength(2);
    expect(cards.find(c => c.id === 'c1')?.title).toBe('new');
    expect(cards.find(c => c.id === 'c1')?.status).toBe('in_progress');
  });

  it('never adds unknown cards (WS race protection)', async () => {
    await seed([makeCard({ id: 'c1' })]);

    cardsStore.updateLocal(makeCard({ id: 'unseen' }));

    expect(get(cardsStore)).toHaveLength(1);
    expect(get(cardsStore)[0].id).toBe('c1');
  });
});

describe('cardsStore.deleteLocal', () => {
  it('removes only the targeted card', async () => {
    await seed([makeCard({ id: 'c1' }), makeCard({ id: 'c2' })]);

    cardsStore.deleteLocal('c1');

    expect(get(cardsStore).map(c => c.id)).toEqual(['c2']);
  });

  it('is a no-op for an unknown id', async () => {
    await seed([makeCard({ id: 'c1' })]);
    cardsStore.deleteLocal('nope');
    expect(get(cardsStore)).toHaveLength(1);
  });
});

describe('cardsByStatus', () => {
  it('groups cards into their status columns', async () => {
    const statuses: CardStatus[] = ['todo', 'in_progress', 'in_review', 'done', 'failed'];
    await seed(statuses.map((status, i) => makeCard({ id: `c${i}`, status })));

    const grouped = get(cardsByStatus);
    expect(grouped.todo).toHaveLength(1);
    expect(grouped.in_progress).toHaveLength(1);
    expect(grouped.in_review).toHaveLength(1);
    expect(grouped.done).toHaveLength(1);
    expect(grouped.failed).toHaveLength(1);
    expect(grouped.todo[0].id).toBe('c0');
  });

  it('tracks status changes applied through updateLocal', async () => {
    await seed([makeCard({ id: 'c1', status: 'todo' })]);

    cardsStore.updateLocal(makeCard({ id: 'c1', status: 'in_review' }));

    const grouped = get(cardsByStatus);
    expect(grouped.todo).toHaveLength(0);
    expect(grouped.in_review.map(c => c.id)).toEqual(['c1']);
  });

  it('returns all-empty columns after clear()', async () => {
    await seed([makeCard({ id: 'c1' })]);
    cardsStore.clear();

    const grouped = get(cardsByStatus);
    for (const column of Object.values(grouped)) {
      expect(column).toEqual([]);
    }
  });
});

describe('cardsStore.load error handling', () => {
  it('records the error message and leaves the store usable', async () => {
    vi.mocked(cardsApi.list).mockRejectedValueOnce(new Error('boom'));
    await cardsStore.load('repo-1');

    expect(get(cardsStore.error)).toBe('boom');
    expect(get(cardsStore)).toEqual([]);
    expect(get(cardsStore.loading)).toBe(false);
  });
});
