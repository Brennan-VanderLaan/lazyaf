/**
 * THE one place timestamps become numbers and numbers become text.
 *
 * WHY THIS MODULE EXISTS
 * ----------------------
 * The same bug was reported four times from four surfaces (QA triage T1):
 * `PipelinesPage`, `PipelineRunViewer`, `JobStatus` and `RunnerPanel` each
 * carried their own copy of `new Date(str).getTime()` + "seconds < 60" and
 * each rendered the same lie — `-14399s` live durations, a "Started" column
 * hours in the future, a runner frozen at `ws 0s`, and `NaNm NaNs` for
 * anything unparseable. `BranchManager` and `RepoInfo` carried a second,
 * byte-identical pair of relative-time formatters.
 *
 * Six copies, one behaviour. This module is that behaviour, and every one of
 * those call sites now delegates here.
 *
 * THE WIRE CONTRACT (standing rule R3: one source of truth)
 * ---------------------------------------------------------
 * The backend emits ISO-8601 UTC. It is being changed to carry an explicit
 * designator (`...Z` / `...+00:00`); until every row written before that
 * change has aged out, naive strings (`2026-08-30T12:06:32.695487`) remain on
 * the wire from the database.
 *
 * ECMA-262 parses a naive date-TIME string as LOCAL time, which is exactly
 * how a row created one second ago rendered four hours in the future on a
 * UTC-4 laptop. `parseTimestamp` therefore treats a naive string as UTC —
 * which is what it has always meant — so this code is correct against both
 * the new aware wire format and every legacy naive value already stored.
 *
 * NOTHING HERE INVENTS A VALUE (standing rule R1: no silent fallbacks)
 * -------------------------------------------------------------------
 * An unparseable or impossibly-ordered timestamp renders `UNKNOWN` ('—'), not
 * `0s`. A clamp-to-zero is how `RunnerPanel.connectionAge` turned a four-hour
 * error into a permanent, plausible-looking `ws 0s` that nobody noticed for
 * four QA passes. Small negatives ARE clamped — a couple of seconds of skew
 * between a backend container's clock and the browser's is real and benign —
 * but only within `CLOCK_SKEW_TOLERANCE_MS`; past that the value is wrong and
 * says so.
 */

/** Rendered whenever there is no honest number to show. Never `0`, never `NaN`. */
export const UNKNOWN = '—';

/**
 * Backend-vs-browser clock difference we will absorb rather than report.
 * A running step whose `started_at` is 400ms in the browser's future is a
 * clock, not a bug; four hours is a bug.
 */
export const CLOCK_SKEW_TOLERANCE_MS = 5_000;

/** ISO strings ending in `Z`, `+HH:MM`, `-HHMM`, … already say what zone they are in. */
const HAS_DESIGNATOR = /(?:Z|[+-]\d{2}:?\d{2})$/i;

/** A date-only value (`2026-08-30`) is already UTC per spec — leave it alone. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** Python's `datetime.isoformat()` emits MICROseconds; JS only wants three digits. */
const OVERLONG_FRACTION = /(\.\d{3})\d+/;

/**
 * Normalise a backend timestamp string to something every JS engine parses as
 * the instant the backend meant.
 *
 * Exported for the tests that pin the naive-is-UTC rule; call
 * `parseTimestamp` for real work.
 */
export function normalizeIso(raw: string): string {
  let value = raw.trim();
  if (value === '') return value;

  // `2026-08-30 12:06:32` (a space separator) is not ISO-8601 and Safari
  // rejects it outright.
  value = value.replace(' ', 'T');

  // Microseconds -> milliseconds. V8 tolerates the extra digits; not every
  // engine does, and a silently-NaN date is how `NaNm NaNs` reached a demo.
  value = value.replace(OVERLONG_FRACTION, '$1');

  if (DATE_ONLY.test(value)) return value;
  if (HAS_DESIGNATOR.test(value)) return value;

  // The naive case. The backend has always meant UTC here; say so explicitly
  // rather than letting the browser guess its own zone.
  return `${value}Z`;
}

/**
 * A backend timestamp as epoch milliseconds, or `null` when there is no
 * usable instant in it.
 *
 * Accepts `null`/`undefined`/`''` (a field the backend legitimately left
 * empty) and garbage (a field it should not have), and reports both the same
 * way: there is no time here. Callers render `UNKNOWN`.
 */
export function parseTimestamp(value: string | Date | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) {
    const ms = value.getTime();
    return Number.isFinite(ms) ? ms : null;
  }
  if (typeof value !== 'string') return null;
  const normalized = normalizeIso(value);
  if (normalized === '') return null;
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Epoch SECONDS (git commit times, which are unix seconds, not ISO strings)
 * as epoch milliseconds.
 *
 * Deliberately a separate function rather than a `parseTimestamp` overload
 * that sniffs magnitude: a number is only unambiguous when the caller says
 * which unit it is in.
 */
export function fromEpochSeconds(seconds: number | null | undefined): number | null {
  if (seconds === null || seconds === undefined) return null;
  if (!Number.isFinite(seconds)) return null;
  return seconds * 1000;
}

/**
 * A non-negative span of milliseconds as `45s` / `3m 07s` / `2h 14m`.
 *
 * The `ms`-granularity variant used by the experiment leaderboard lives in
 * `formatMillis`; this one is for wall-clock spans a human is watching tick.
 */
function formatSpan(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/**
 * Reduce two endpoints to a non-negative span, or `null` when no honest span
 * exists. `end === null` means "still running", i.e. up to `now`.
 */
function spanMs(
  start: string | Date | null | undefined,
  end: string | Date | null | undefined,
  now: number,
): number | null {
  const startMs = parseTimestamp(start);
  if (startMs === null) return null;

  const endMs = end === null || end === undefined ? now : parseTimestamp(end);
  if (endMs === null) return null;

  const delta = endMs - startMs;
  if (delta < 0) {
    // Benign skew is absorbed; a genuinely inverted interval is not made to
    // look like a fresh start.
    return delta >= -CLOCK_SKEW_TOLERANCE_MS ? 0 : null;
  }
  return delta;
}

/**
 * How long something took, or has been running for.
 *
 * `formatDuration(startedAt, completedAt)` for a finished thing;
 * `formatDuration(startedAt, null)` for a live one, which counts up against
 * `now`. Never negative, never `NaN`, never a fabricated `0s`.
 */
export function formatDuration(
  start: string | Date | null | undefined,
  end: string | Date | null | undefined,
  now: number = Date.now(),
): string {
  const ms = spanMs(start, end, now);
  return ms === null ? UNKNOWN : formatSpan(ms);
}

/**
 * How long ago an instant was, as a single coarse unit: `12s` / `4m` / `3h` /
 * `9d`. The runner panel's "ws 4m" reads this.
 *
 * A timestamp in the future beyond clock-skew tolerance is `UNKNOWN` — the
 * previous `Math.max(0, …)` here is precisely what hid T1 behind a permanent
 * `ws 0s`.
 */
export function formatAge(
  value: string | Date | number | null | undefined,
  now: number = Date.now(),
): string {
  const at = typeof value === 'number' ? value : parseTimestamp(value);
  if (at === null || !Number.isFinite(at)) return UNKNOWN;

  const delta = now - at;
  if (delta < 0 && delta < -CLOCK_SKEW_TOLERANCE_MS) return UNKNOWN;

  const seconds = Math.max(0, Math.floor(delta / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

/**
 * How long ago, in prose: `just now` / `4m ago` / `3h ago` / `2d ago`, and an
 * absolute local date once it is a week old (relative loses all meaning past
 * that point).
 *
 * `value` may be epoch milliseconds so callers holding a git commit time can
 * pass `fromEpochSeconds(t)` straight through.
 */
export function formatRelative(
  value: string | Date | number | null | undefined,
  now: number = Date.now(),
): string {
  const at = typeof value === 'number' ? value : parseTimestamp(value);
  if (at === null || !Number.isFinite(at)) return UNKNOWN;

  const delta = now - at;
  if (delta < -CLOCK_SKEW_TOLERANCE_MS) return UNKNOWN;

  const seconds = Math.max(0, Math.floor(delta / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(at).toLocaleDateString();
}

/**
 * An absolute local wall-clock rendering: `8/30/2026, 8:06 AM`.
 *
 * The value is parsed as UTC (see `parseTimestamp`) and then shown in the
 * viewer's own zone, which is the only rendering that is not a lie on a
 * machine that is not on UTC.
 */
export function formatDateTime(value: string | Date | null | undefined): string {
  const at = parseTimestamp(value);
  if (at === null) return UNKNOWN;
  const date = new Date(at);
  return `${date.toLocaleDateString()}, ${date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  })}`;
}

/**
 * Sort key for "newest first" lists. Rows with no usable timestamp sort last
 * rather than jumping to the top on a `NaN` comparison.
 */
export function timestampOrder(value: string | Date | null | undefined): number {
  return parseTimestamp(value) ?? Number.NEGATIVE_INFINITY;
}

/**
 * A raw millisecond count as a short duration — `450ms` / `1.5s` / `2m 05s`.
 *
 * Distinct from `formatDuration` because the input is already a span (an
 * aggregated median, not two endpoints) and sub-second precision is the point
 * of showing it at all. The experiment leaderboard is the only caller.
 */
export function formatMillis(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return UNKNOWN;
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}
