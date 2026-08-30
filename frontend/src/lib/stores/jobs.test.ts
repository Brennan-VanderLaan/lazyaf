/**
 * Job store: "which job is the current one for this card".
 *
 * The store holds the same moment in two spellings — `created_at` as the API
 * serialises it (naive UTC, microsecond precision) and the
 * `new Date().toISOString()` that `updateFromWebSocket` stamps on a job first
 * seen over the socket (aware, `Z`-suffixed, millisecond precision).
 *
 * Comparing those with `>` on the raw strings is comparing SPELLINGS. It
 * happens to agree with time order most of the time, because ISO-8601 sorts
 * lexicographically — and then disagrees exactly where the two spellings
 * differ in the fraction: `'…00.123Z' > '…00.123456'` is `true` because 'Z'
 * outranks '4'. The comparison now goes through `utils/time.timestampOrder`,
 * which answers the question actually being asked: which job is newer.
 *
 * `jobsApi.get` is the seam (standing rule R6): jobs enter through the real
 * HTTP client the store calls, not through a hand-poked internal map.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';

import type { Job } from '../api/types';

const getMock = vi.fn();

// Partial mock: only the verb the store calls is faked.
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  jobs: { get: (...args: unknown[]) => getMock(...args) },
}));

const { jobsStore, getJobForCard } = await import('./jobs');

function makeJob(id: string, cardId: string, createdAt: string): Job {
  return {
    id,
    card_id: cardId,
    runner_id: null,
    status: 'queued',
    logs: '',
    error: null,
    started_at: null,
    completed_at: null,
    created_at: createdAt,
  } as Job;
}

/** Load a job through the store's real API path. */
async function seed(id: string, cardId: string, createdAt: string) {
  getMock.mockResolvedValueOnce(makeJob(id, cardId, createdAt));
  await jobsStore.load(id);
}

beforeEach(() => {
  getMock.mockReset();
  jobsStore.clear();
});

describe('getByCardId picks the newest job by INSTANT, not by string order', () => {
  it('prefers a later naive timestamp over an earlier aware one', async () => {
    await seed('older', 'card-1', '2026-08-30T12:00:00.000000Z');
    await seed('newer', 'card-1', '2026-08-30T12:05:00.000000');

    expect(jobsStore.getByCardId('card-1')?.id).toBe('newer');
    expect(get(getJobForCard('card-1'))?.id).toBe('newer');
  });

  it('prefers a later aware timestamp over an earlier naive one', async () => {
    await seed('older', 'card-2', '2026-08-30T12:00:00.000000');
    await seed('newer', 'card-2', '2026-08-30T12:05:00.000000Z');

    expect(jobsStore.getByCardId('card-2')?.id).toBe('newer');
    expect(get(getJobForCard('card-2'))?.id).toBe('newer');
  });

  it('does not let a trailing Z outrank a digit at the same instant', async () => {
    // The precise case where string order and time order disagree: these two
    // are the same millisecond, but `'…00.123Z' > '…00.123456'` is true, so
    // the string comparison promoted the second-seen row over the first for
    // no reason but its punctuation. Equal instants must not reorder anything.
    await seed('first-seen', 'card-3', '2026-08-30T12:00:00.123456');
    await seed('same-instant', 'card-3', '2026-08-30T12:00:00.123Z');

    expect('2026-08-30T12:00:00.123Z' > '2026-08-30T12:00:00.123456').toBe(true);
    expect(jobsStore.getByCardId('card-3')?.id).toBe('first-seen');
  });

  it('ignores jobs belonging to other cards', async () => {
    await seed('mine', 'card-4', '2026-08-30T12:00:00');
    await seed('theirs', 'card-5', '2026-08-30T13:00:00');

    expect(jobsStore.getByCardId('card-4')?.id).toBe('mine');
  });

  it('returns undefined when the card has no job', () => {
    expect(jobsStore.getByCardId('nobody')).toBeUndefined();
    expect(get(getJobForCard('nobody'))).toBeUndefined();
  });
});
