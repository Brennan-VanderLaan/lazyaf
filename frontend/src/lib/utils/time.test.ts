/**
 * Regression pins for QA triage T1 — "every timestamp is naive UTC".
 *
 * The bug shipped four times because the arithmetic was copy-pasted into four
 * components and nothing tested any copy. These tests are the reason there is
 * now one copy.
 *
 * The vitest process runs on the host's own zone, so the naive-is-UTC rule is
 * asserted against absolute epoch values rather than rendered local strings —
 * a test that only passes on a UTC machine would be no test at all.
 */
import { describe, it, expect } from 'vitest';

import {
  CLOCK_SKEW_TOLERANCE_MS,
  UNKNOWN,
  formatAge,
  formatDateTime,
  formatDuration,
  formatMillis,
  formatRelative,
  fromEpochSeconds,
  normalizeIso,
  parseTimestamp,
  timestampOrder,
} from './time';

/** 2026-08-30T12:06:32.695Z — the exact value from the QA reproduction. */
const NAIVE_FROM_BACKEND = '2026-08-30T12:06:32.695487';
const INSTANT = Date.UTC(2026, 7, 30, 12, 6, 32, 695);

describe('parseTimestamp: the naive-UTC wire contract', () => {
  it('reads a naive backend timestamp as UTC, not as browser-local time', () => {
    // THE bug: `new Date('2026-08-30T12:06:32.695487')` is 12:06 LOCAL, which
    // on the QA machine (UTC-4) is 16:06 UTC — four hours in the future.
    expect(parseTimestamp(NAIVE_FROM_BACKEND)).toBe(INSTANT);
    // …and it lands on the same instant as the aware spelling of the same
    // value, which is the whole property, on a host in any zone.
    expect(parseTimestamp(NAIVE_FROM_BACKEND)).toBe(
      parseTimestamp(`${NAIVE_FROM_BACKEND}Z`),
    );
  });

  it('the raw `new Date(str)` path this replaces is off by the host offset', () => {
    // Not a tautology: it pins WHY the module exists, and is meaningful on
    // any machine that is not on UTC (which is every demo laptop).
    const offsetMs = new Date(INSTANT).getTimezoneOffset() * 60_000;
    expect(new Date(NAIVE_FROM_BACKEND).getTime()).toBe(INSTANT + offsetMs);
  });

  it('accepts the aware forms the backend is moving to', () => {
    expect(parseTimestamp('2026-08-30T12:06:32.695487Z')).toBe(INSTANT);
    expect(parseTimestamp('2026-08-30T12:06:32.695487+00:00')).toBe(INSTANT);
    // A real offset must be honoured, not overwritten with Z.
    expect(parseTimestamp('2026-08-30T08:06:32.695-04:00')).toBe(INSTANT);
    expect(parseTimestamp('2026-08-30T08:06:32.695-0400')).toBe(INSTANT);
  });

  it('truncates Python microseconds instead of risking an engine-specific NaN', () => {
    expect(normalizeIso(NAIVE_FROM_BACKEND)).toBe('2026-08-30T12:06:32.695Z');
    expect(normalizeIso('2026-08-30T12:06:32.695487Z')).toBe('2026-08-30T12:06:32.695Z');
  });

  it('accepts a space separator (not ISO, and rejected outright by Safari)', () => {
    expect(parseTimestamp('2026-08-30 12:06:32.695487')).toBe(INSTANT);
  });

  it('leaves a date-only value alone (already UTC per spec)', () => {
    expect(normalizeIso('2026-08-30')).toBe('2026-08-30');
    expect(parseTimestamp('2026-08-30')).toBe(Date.UTC(2026, 7, 30));
  });

  it('returns null — never NaN — for absent or unusable values', () => {
    expect(parseTimestamp(null)).toBeNull();
    expect(parseTimestamp(undefined)).toBeNull();
    expect(parseTimestamp('')).toBeNull();
    expect(parseTimestamp('   ')).toBeNull();
    expect(parseTimestamp('not a date')).toBeNull();
    expect(parseTimestamp(new Date(Number.NaN))).toBeNull();
  });

  it('passes a Date through', () => {
    expect(parseTimestamp(new Date(INSTANT))).toBe(INSTANT);
  });
});

describe('fromEpochSeconds', () => {
  it('converts unix seconds (git commit times) to milliseconds', () => {
    expect(fromEpochSeconds(1_756_555_592)).toBe(1_756_555_592_000);
  });

  it('returns null for absent or non-finite input', () => {
    expect(fromEpochSeconds(null)).toBeNull();
    expect(fromEpochSeconds(undefined)).toBeNull();
    expect(fromEpochSeconds(Number.NaN)).toBeNull();
    expect(fromEpochSeconds(Number.POSITIVE_INFINITY)).toBeNull();
  });
});

describe('formatDuration', () => {
  it('counts a live run up from a naive start, on a non-UTC browser', () => {
    // This is the demo failure verbatim: PipelinesPage rendered `-14399s`.
    const now = INSTANT + 42_000;
    expect(formatDuration(NAIVE_FROM_BACKEND, null, now)).toBe('42s');
  });

  it('renders a finished span between two endpoints', () => {
    expect(
      formatDuration(NAIVE_FROM_BACKEND, '2026-08-30T12:09:14.695487', INSTANT),
    ).toBe('2m 42s');
  });

  it('pads seconds so the column does not jitter between 2m 9s and 2m 10s', () => {
    expect(formatDuration(NAIVE_FROM_BACKEND, null, INSTANT + 129_000)).toBe('2m 09s');
  });

  it('rolls over to hours instead of rendering 214m 3s', () => {
    expect(formatDuration(NAIVE_FROM_BACKEND, null, INSTANT + 7_442_000)).toBe('2h 04m');
  });

  it('NEVER renders a negative duration', () => {
    const result = formatDuration(NAIVE_FROM_BACKEND, null, INSTANT - 14_400_000);
    expect(result).toBe(UNKNOWN);
    expect(result).not.toMatch(/-/);
  });

  it('absorbs benign clock skew as 0s rather than shouting', () => {
    expect(formatDuration(NAIVE_FROM_BACKEND, null, INSTANT - 1_000)).toBe('0s');
    expect(
      formatDuration(NAIVE_FROM_BACKEND, null, INSTANT - CLOCK_SKEW_TOLERANCE_MS),
    ).toBe('0s');
  });

  it('does NOT clamp a real inversion into a plausible 0s', () => {
    // The `Math.max(0, …)` failure mode: a four-hour error rendered as a
    // permanent, believable zero that nobody investigated.
    expect(
      formatDuration(NAIVE_FROM_BACKEND, null, INSTANT - CLOCK_SKEW_TOLERANCE_MS - 1),
    ).toBe(UNKNOWN);
  });

  it('NEVER renders NaN for an unparseable endpoint', () => {
    expect(formatDuration('garbage', null, INSTANT)).toBe(UNKNOWN);
    expect(formatDuration(NAIVE_FROM_BACKEND, 'garbage', INSTANT)).toBe(UNKNOWN);
    expect(formatDuration(null, null, INSTANT)).toBe(UNKNOWN);
    expect(formatDuration(undefined, undefined, INSTANT)).toBe(UNKNOWN);
  });
});

describe('formatAge', () => {
  it('reports a runner that connected an hour ago as 1h, not ws 0s', () => {
    // RunnerPanel.connectionAge froze at `ws 0s` for exactly this input.
    expect(formatAge(NAIVE_FROM_BACKEND, INSTANT + 3_600_000)).toBe('1h');
  });

  it('steps through the coarse units', () => {
    expect(formatAge(NAIVE_FROM_BACKEND, INSTANT + 12_000)).toBe('12s');
    expect(formatAge(NAIVE_FROM_BACKEND, INSTANT + 240_000)).toBe('4m');
    expect(formatAge(NAIVE_FROM_BACKEND, INSTANT + 9 * 86_400_000)).toBe('9d');
  });

  it('says UNKNOWN for a value from the future rather than a fake 0s', () => {
    expect(formatAge(NAIVE_FROM_BACKEND, INSTANT - 14_400_000)).toBe(UNKNOWN);
  });

  it('absorbs benign skew', () => {
    expect(formatAge(NAIVE_FROM_BACKEND, INSTANT - 2_000)).toBe('0s');
  });

  it('says UNKNOWN for absent or unparseable input', () => {
    expect(formatAge(null)).toBe(UNKNOWN);
    expect(formatAge('')).toBe(UNKNOWN);
    expect(formatAge('nope')).toBe(UNKNOWN);
  });

  it('accepts epoch milliseconds directly', () => {
    expect(formatAge(INSTANT, INSTANT + 5_000)).toBe('5s');
  });
});

describe('formatRelative', () => {
  it('says "just now" under a minute instead of 0m ago', () => {
    expect(formatRelative(NAIVE_FROM_BACKEND, INSTANT + 30_000)).toBe('just now');
  });

  it('steps minutes, hours and days', () => {
    expect(formatRelative(NAIVE_FROM_BACKEND, INSTANT + 300_000)).toBe('5m ago');
    expect(formatRelative(NAIVE_FROM_BACKEND, INSTANT + 7_200_000)).toBe('2h ago');
    expect(formatRelative(NAIVE_FROM_BACKEND, INSTANT + 3 * 86_400_000)).toBe('3d ago');
  });

  it('falls back to an absolute date past a week', () => {
    const at = INSTANT + 30 * 86_400_000;
    expect(formatRelative(NAIVE_FROM_BACKEND, at)).toBe(
      new Date(INSTANT).toLocaleDateString(),
    );
  });

  it('accepts a git commit time via fromEpochSeconds', () => {
    const seconds = INSTANT / 1000;
    expect(formatRelative(fromEpochSeconds(seconds), INSTANT + 600_000)).toBe('10m ago');
  });

  it('says UNKNOWN rather than "just now" for garbage', () => {
    expect(formatRelative('nope')).toBe(UNKNOWN);
    expect(formatRelative(null)).toBe(UNKNOWN);
    expect(formatRelative(fromEpochSeconds(null))).toBe(UNKNOWN);
  });
});

describe('formatDateTime', () => {
  it('renders the naive backend instant in the viewer local zone', () => {
    const expected = new Date(INSTANT);
    expect(formatDateTime(NAIVE_FROM_BACKEND)).toBe(
      `${expected.toLocaleDateString()}, ${expected.toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
      })}`,
    );
  });

  it('never renders "Invalid Date"', () => {
    expect(formatDateTime('nope')).toBe(UNKNOWN);
    expect(formatDateTime(null)).toBe(UNKNOWN);
    expect(formatDateTime(undefined)).toBe(UNKNOWN);
  });
});

describe('timestampOrder', () => {
  it('sorts newest first without a NaN comparison reordering the list', () => {
    const rows = [
      { id: 'old', created_at: '2026-08-30T10:00:00' },
      { id: 'broken', created_at: 'nonsense' },
      { id: 'new', created_at: '2026-08-30T12:00:00' },
    ];
    const sorted = [...rows].sort(
      (a, b) => timestampOrder(b.created_at) - timestampOrder(a.created_at),
    );
    expect(sorted.map((r) => r.id)).toEqual(['new', 'old', 'broken']);
  });
});

describe('formatMillis', () => {
  it('keeps the leaderboard rendering it already had', () => {
    expect(formatMillis(450)).toBe('450ms');
    expect(formatMillis(1500)).toBe('1.5s');
    expect(formatMillis(125_000)).toBe('2m 5s');
  });

  it('renders UNKNOWN for absent or impossible spans', () => {
    expect(formatMillis(null)).toBe(UNKNOWN);
    expect(formatMillis(undefined)).toBe(UNKNOWN);
    expect(formatMillis(Number.NaN)).toBe(UNKNOWN);
    expect(formatMillis(-1)).toBe(UNKNOWN);
  });
});
