/**
 * Diagnostics store - the Logs tab and the bug-report bundle (Lane C, Phase 1).
 *
 * WHAT THIS IS FOR
 * ----------------
 * The expensive question when this platform misbehaves is not "what did the
 * step print", it is "WHAT IS ACTUALLY RUNNING HERE". A backend container
 * serving 25-hour-old code off a bind mount that has been current for a day
 * produces a "branch not found" that is indistinguishable, from inside the
 * product, from a real git bug. Nothing in the UI said so. The system strip
 * this store feeds exists to say so in one line, above the logs, before the
 * user has selected anything.
 *
 * THE BACKEND CONTRACT THIS LANE ASSUMES
 * --------------------------------------
 * None of these routes exist yet - `backend/app/routers/diagnostics.py` is
 * Phase 1 backend work. This module is the client half, written against the
 * design, and it DEGRADES HONESTLY when the routes are absent (see
 * `DIAGNOSTICS_UNAVAILABLE` below) rather than rendering a green system strip
 * for a backend that never answered. A green strip we invented would be the
 * exact "fake green" R4 forbids, and it would be green about the one fact the
 * whole feature exists to report.
 *
 *   GET  /api/diagnostics/system                  -> SystemFacts
 *   GET  /api/diagnostics/runs                    -> RunOption[]
 *   GET  /api/diagnostics/logs?run_id=&after=     -> LogPage
 *   POST /api/diagnostics/bundle                  -> BundleManifest
 *   GET  /api/diagnostics/bundle/{id}             -> application/zip
 *   GET  /api/diagnostics/bundle/{id}/members/{n} -> text/plain (redacted)
 *
 * REDACTION IS THE SERVER'S JOB, NOT OURS
 * ---------------------------------------
 * Every `message` on the wire is ALREADY redacted by the backend `Redactor`.
 * This module never sees a raw secret and must never try to redact one - a
 * second, client-side redactor would be a fourth copy of the pattern set (R3)
 * and would drift within a week. What we do here is make the redaction
 * VISIBLE: `splitRedactions` marks the placeholder spans so the user can see
 * the safety net working on screen, which is also what makes it reviewable
 * before anything reaches a public GitHub issue.
 *
 * WHY THIS FILE CARRIES ITS OWN TYPES AND ITS OWN `fetch`
 * ------------------------------------------------------
 * `api/client.ts` and `api/types.ts` belong to another lane in this change.
 * Rather than edit them, the diagnostics wire types live here and requests go
 * through a local helper that reuses the SHARED `ApiError` - so a diagnostics
 * failure carries the same status/detail shape as every other call in the app
 * and the eventual move into `client.ts` is a cut-and-paste with no behaviour
 * change.
 */
import { writable, derived, get, type Readable } from 'svelte/store';
import { ApiError, NETWORK_ERROR_STATUS } from '../api/client';

// =============================================================================
// Wire types
// =============================================================================

/**
 * Source A - the code-vs-process staleness probe. THE decisive fact.
 *
 * `stale` is true when a Python file under the app root is NEWER than the
 * process that imported it, which means the files on disk are not the code
 * being executed. This is the 25-hour-container bug, named.
 */
export interface StalenessFacts {
  stale: boolean;
  process_started_at: string;
  newest_file_mtime: string | null;
  stale_file_count: number;
  stale_files: string[];
  /** The command that fixes it, from the server (it knows its own service name). */
  remedy: string | null;
}

/** Source D - alembic head in the database vs. the revision graph on disk. */
export interface MigrationFacts {
  ok: boolean;
  db_head: string | null;
  disk_heads: string[];
  unapplied_count: number;
  /** Breaks in the revision chain, e.g. the real 0007 -> 0009 gap on main. */
  gaps: string[];
}

/**
 * Source E - git HEAD of the checkout.
 *
 * `available` is false in the shipped container: compose bind-mounts
 * `backend/app` and `backend/alembic` and nothing else, so there is no `.git`
 * to read. `reason` says that in words. We do NOT substitute a build-time
 * baked SHA - with a bind mount that value is a lie by construction (the
 * image was built at X while the files executing are at Y).
 */
export interface CheckoutFacts {
  available: boolean;
  sha: string | null;
  dirty: boolean | null;
  dirty_file_count: number | null;
  reason: string | null;
}

/**
 * Source B - one LazyAF container. Field-ALLOWLISTED server-side.
 *
 * There is deliberately no `env`, no `cmd` and no `mounts` here, and the
 * absence is load-bearing: `docker inspect` returns `Config.Env`, which on
 * this project's backend container holds LAZYAF_STEP_AUTH_SECRET,
 * LAZYAF_RUNNER_AUTH_SECRET and ANTHROPIC_API_KEY. A denylist over that
 * object is one new docker field away from leaking; an allowlist cannot be.
 */
export interface ContainerFacts {
  name: string;
  service: string | null;
  image: string;
  image_id: string;
  started_at: string;
  restart_count: number;
  health: string | null;
}

export interface SystemFacts {
  generated_at: string;
  staleness: StalenessFacts;
  migrations: MigrationFacts;
  checkout: CheckoutFacts;
  containers: ContainerFacts[];
  /**
   * Count only, never names. This machine runs containers belonging to other
   * projects; publishing their names into a public issue would leak the
   * existence of unrelated work for no diagnostic gain.
   */
  other_containers_on_host: number;
  /** Set when the docker socket could not be read at all. R1: say so. */
  container_error: string | null;
}

export type LogSourceKind = 'backend' | 'step' | 'runner' | 'browser';

/**
 * One line in the merged stream.
 *
 * `id` is the identity; `seq` is the SERVER's cursor and is absent on
 * locally-captured browser records. Keeping them separate is what lets a
 * browser error be appended live without corrupting the fetch cursor.
 */
export interface LogEntry {
  id: string;
  seq: number | null;
  ts: string;
  /**
   * The SOURCE ID, and it must equal a `LogSourceSummary.id` - the rail
   * filters on exactly this string. It doubles as the pane's prefix column,
   * so it is written to read well there: `backend`, `runner-agent`,
   * `step[2]`, `browser`.
   */
  source: string;
  kind: LogSourceKind;
  level: string;
  /** ALREADY REDACTED by the server. Never contains a live secret. */
  message: string;
}

export interface LogSourceSummary {
  id: string;
  label: string;
  kind: LogSourceKind;
  count: number;
  errors: number;
  /**
   * True for agent step logs and runner logs - the sources that carry model
   * output, prompts and diffs of the user's source, none of which pattern
   * redaction can touch. The UI labels these in words, every time.
   */
  risky: boolean;
}

export interface LogPage {
  entries: Omit<LogEntry, 'id'>[];
  cursor: number;
  sources: LogSourceSummary[];
}

export interface RunOption {
  id: string;
  label: string;
  status: string;
  started_at: string | null;
  has_failed_step: boolean;
}

export type BundleRisk = 'none' | 'low' | 'high';

export interface BundleMember {
  name: string;
  /**
   * The source id that produced this member, from the review sheet's source
   * allowlist. The sheet joins on it so a member's real size and redaction
   * count render on the row whose checkbox controls it - one list showing
   * server truth, rather than a wishlist beside a result.
   */
  source: string;
  bytes: number;
  /** One line of what is in it: "412 records - 3 redacted". */
  summary: string;
  redacted: number;
  risk: BundleRisk;
  /** Present on every `high` member, and shown in red next to it. */
  risk_note: string | null;
}

/**
 * A BUILT bundle. Not a plan for one.
 *
 * The id addresses bytes that already exist on the server. The review sheet
 * renders THIS object's members and Download streams THIS id, so there is no
 * second code path between what the user reviewed and what they got. Any
 * change to the selection builds a new bundle with a new id.
 */
export interface BundleManifest {
  id: string;
  created_at: string;
  expires_at: string;
  total_bytes: number;
  redacted_total: number;
  members: BundleMember[];
}

export interface BundleRequest {
  title: string;
  what_happened: string;
  /** Source ids to include. An ALLOWLIST - the bundle never widens on its own. */
  sources: string[];
  run_id: string | null;
}

// =============================================================================
// Redaction placeholders
// =============================================================================

/**
 * `[REDACTED:anthropic:9c41a2]` - label, then a salted digest.
 *
 * The digest is HMAC(per-bundle random salt, value) and the salt is discarded,
 * so it carries zero bits of the secret while still letting you see that the
 * token in the runner log and the one in the step log are the SAME object -
 * which is what distinguishes a routing bug from a key bug. There is
 * deliberately no length and no prefix in the placeholder.
 */
export const REDACTION_PLACEHOLDER =
  /\[REDACTED:([A-Za-z0-9_.-]+)(?::([0-9a-f]{4,32}))?\]/g;

export interface MessageSegment {
  text: string;
  redacted: boolean;
  label: string | null;
}

/**
 * Split a message into plain and redacted spans so the UI can highlight the
 * latter. Purely presentational - it never changes a character of the text.
 *
 * Highlighting matters more than it looks: it is the only on-screen evidence
 * that redaction ran at all. A bundle that silently failed to redact and one
 * that had nothing to redact are indistinguishable without it.
 */
export function splitRedactions(message: string): MessageSegment[] {
  const segments: MessageSegment[] = [];
  let last = 0;
  // A fresh regex per call: a module-level /g regex carries `lastIndex`
  // between calls and would skip every other match under concurrent use.
  const re = new RegExp(REDACTION_PLACEHOLDER.source, 'g');
  let match: RegExpExecArray | null;
  while ((match = re.exec(message)) !== null) {
    if (match.index > last) {
      segments.push({ text: message.slice(last, match.index), redacted: false, label: null });
    }
    segments.push({ text: match[0], redacted: true, label: match[1] ?? null });
    last = match.index + match[0].length;
  }
  if (last < message.length) {
    segments.push({ text: message.slice(last), redacted: false, label: null });
  }
  if (segments.length === 0) {
    segments.push({ text: message, redacted: false, label: null });
  }
  return segments;
}

/** How many redaction placeholders a message carries. */
export function countRedactions(message: string): number {
  const re = new RegExp(REDACTION_PLACEHOLDER.source, 'g');
  let n = 0;
  while (re.exec(message) !== null) n++;
  return n;
}

// =============================================================================
// Transport
// =============================================================================

const BASE = '/api/diagnostics';

/**
 * A 404 from the diagnostics router means the BACKEND PREDATES THIS FEATURE,
 * which is a completely different situation from "the probe failed" and must
 * not be rendered as one. It gets its own state so the page can say "this
 * backend does not expose /api/diagnostics" and name the routes it needs,
 * instead of showing an empty-but-plausible system strip.
 */
export const DIAGNOSTICS_UNAVAILABLE = 'diagnostics-unavailable';

async function diagRequest<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json', ...options?.headers },
      ...options,
    });
  } catch (e) {
    throw new ApiError(
      NETWORK_ERROR_STATUS,
      `Cannot reach the LazyAF backend (${e instanceof Error ? e.message : 'network error'})`,
      e,
    );
  }
  if (!response.ok) {
    let raw = '';
    try {
      raw = await response.text();
    } catch {
      raw = '';
    }
    let detail: unknown = null;
    try {
      const parsed = JSON.parse(raw);
      detail = parsed && typeof parsed === 'object' && 'detail' in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    } catch {
      detail = raw || null;
    }
    const described = typeof detail === 'string' && detail.trim() ? detail : null;
    throw new ApiError(
      response.status,
      described ? `${described} (HTTP ${response.status})` : `HTTP ${response.status}`,
      detail,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** True when the failure means "no such route", not "the route failed". */
function isMissingRoute(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

function describe(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

export const diagnosticsApi = {
  system: () => diagRequest<SystemFacts>('/system'),
  runs: () => diagRequest<RunOption[]>('/runs'),
  logs: (params: { runId?: string | null; after?: number | null; limit?: number }) => {
    const q = new URLSearchParams();
    if (params.runId) q.set('run_id', params.runId);
    if (params.after != null) q.set('after', String(params.after));
    if (params.limit != null) q.set('limit', String(params.limit));
    const suffix = q.toString() ? `?${q}` : '';
    return diagRequest<LogPage>(`/logs${suffix}`);
  },
  buildBundle: (body: BundleRequest) =>
    diagRequest<BundleManifest>('/bundle', { method: 'POST', body: JSON.stringify(body) }),
  member: async (id: string, name: string): Promise<string> => {
    const response = await fetch(
      `${BASE}/bundle/${encodeURIComponent(id)}/members/${encodeURIComponent(name)}`,
    );
    if (!response.ok) {
      throw new ApiError(response.status, `Could not read ${name} (HTTP ${response.status})`);
    }
    return response.text();
  },
  /**
   * Fetch the archive as bytes rather than pointing an `<a download>` at the
   * URL, so an expired bundle (the TTL is 15 minutes) surfaces as a sentence
   * the user can act on instead of a browser error page on a dead link.
   */
  archive: async (id: string): Promise<Blob> => {
    const response = await fetch(`${BASE}/bundle/${encodeURIComponent(id)}`);
    if (!response.ok) {
      throw new ApiError(
        response.status,
        response.status === 404
          ? 'This bundle has expired (bundles are kept in memory for 15 minutes). Build a new one.'
          : `Download failed (HTTP ${response.status})`,
      );
    }
    return response.blob();
  },
};

// =============================================================================
// Store
// =============================================================================

export type LoadStatus = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error';

export interface DiagnosticsState {
  systemStatus: LoadStatus;
  system: SystemFacts | null;
  systemError: string | null;

  logStatus: LoadStatus;
  logError: string | null;
  /**
   * APPEND-ONLY between resets. The Logs pane renders this with an UNKEYED
   * each-block, which index-reconciles and therefore leaves the DOM of
   * already-rendered lines untouched as new ones arrive - that is what lets a
   * drag-selection survive a flush. Replacing this array wholesale with a
   * re-fetched one, or inserting into the middle of it, rewrites the tail and
   * destroys the selection. Every write below either appends or is an
   * explicit, user-initiated reset.
   */
  entries: LogEntry[];
  /** Server cursor. Browser-captured records never advance it. */
  cursor: number | null;
  sources: LogSourceSummary[];
  /** Lines evicted by the cap. Shown, never silent. */
  dropped: number;

  runs: RunOption[];
  selectedRunId: string | null;

  live: boolean;
}

/** Soft cap. Trimming is deferred while the user is reading (see `holdTrim`). */
const MAX_ENTRIES = 5000;
/**
 * Hard cap. A user who scrolls up and walks away must not grow the buffer
 * without bound; past this we trim anyway and the pane says how many went.
 */
const HARD_MAX_ENTRIES = 15000;
const POLL_INTERVAL_MS = 3000;

function initialState(): DiagnosticsState {
  return {
    systemStatus: 'idle',
    system: null,
    systemError: null,
    logStatus: 'idle',
    logError: null,
    entries: [],
    cursor: null,
    sources: [],
    dropped: 0,
    runs: [],
    selectedRunId: null,
    live: true,
  };
}

function createDiagnosticsStore() {
  const { subscribe, update, set } = writable<DiagnosticsState>(initialState());

  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let inFlight = false;
  let localSeq = 0;
  /**
   * Trim suppression, owned by the pane.
   *
   * Evicting from the FRONT of an unkeyed each-block shifts every index and
   * rewrites every line in the DOM - which collapses any standing selection
   * and yanks the scroll position. The pane holds this while the user is
   * scrolled up or has text highlighted, and releases it when they return to
   * the tail. This is the one piece of view state the store needs to know
   * about, and it is here rather than in the component because the trim
   * itself has to happen where the buffer lives.
   */
  let trimHeld = false;

  function trim(state: DiagnosticsState): DiagnosticsState {
    const cap = trimHeld ? HARD_MAX_ENTRIES : MAX_ENTRIES;
    if (state.entries.length <= cap) return state;
    const excess = state.entries.length - MAX_ENTRIES;
    return {
      ...state,
      entries: state.entries.slice(excess),
      dropped: state.dropped + excess,
    };
  }

  function appendServer(state: DiagnosticsState, page: LogPage): DiagnosticsState {
    const fresh = page.entries.map((e) => ({
      ...e,
      id: e.seq != null ? `srv:${e.seq}` : `srv:${e.ts}:${e.source}:${localSeq++}`,
    }));
    return trim({
      ...state,
      entries: fresh.length ? [...state.entries, ...fresh] : state.entries,
      cursor: page.cursor,
      sources: page.sources,
      logStatus: 'ready',
      logError: null,
    });
  }

  async function fetchLogs(reset: boolean): Promise<void> {
    if (inFlight) return;
    inFlight = true;
    try {
      const state = get({ subscribe });
      const page = await diagnosticsApi.logs({
        runId: state.selectedRunId,
        after: reset ? null : state.cursor,
      });
      update((s) => {
        if (reset) {
          // A reset is always a deliberate user action (opening the page,
          // changing the run). Sorting is safe HERE and only here, because
          // nothing is on screen to have a selection in yet.
          const seeded = page.entries
            .map((e) => ({
              ...e,
              id: e.seq != null ? `srv:${e.seq}` : `srv:${e.ts}:${e.source}:${localSeq++}`,
            }))
            .sort((a, b) => a.ts.localeCompare(b.ts));
          return {
            ...s,
            entries: seeded,
            cursor: page.cursor,
            sources: page.sources,
            dropped: 0,
            logStatus: 'ready',
            logError: null,
          };
        }
        return appendServer(s, page);
      });
    } catch (e) {
      update((s) => ({
        ...s,
        logStatus: isMissingRoute(e) ? 'unavailable' : 'error',
        logError: isMissingRoute(e) ? DIAGNOSTICS_UNAVAILABLE : describe(e),
      }));
    } finally {
      inFlight = false;
    }
  }

  return {
    subscribe,

    /** The pane's trim lease. See `trimHeld`. */
    holdTrim(held: boolean) {
      trimHeld = held;
      if (!held) update((s) => trim(s));
    },

    async loadSystem() {
      update((s) => ({ ...s, systemStatus: 'loading', systemError: null }));
      try {
        const system = await diagnosticsApi.system();
        update((s) => ({ ...s, system, systemStatus: 'ready', systemError: null }));
      } catch (e) {
        update((s) => ({
          ...s,
          system: null,
          systemStatus: isMissingRoute(e) ? 'unavailable' : 'error',
          systemError: isMissingRoute(e) ? DIAGNOSTICS_UNAVAILABLE : describe(e),
        }));
      }
    },

    async loadRuns() {
      try {
        const runs = await diagnosticsApi.runs();
        update((s) => ({
          ...s,
          runs,
          // Seed with the most recent failed run, else the most recent run.
          // A user arriving here after a failure is almost always looking for
          // that failure, and making them find it in a dropdown first is a
          // step between them and the answer.
          selectedRunId:
            s.selectedRunId ?? runs.find((r) => r.has_failed_step)?.id ?? runs[0]?.id ?? null,
        }));
      } catch {
        // A missing run list is not worth an error banner - the merged stream
        // still carries backend and runner output, which is most of the value.
        update((s) => ({ ...s, runs: [] }));
      }
    },

    async selectRun(runId: string | null) {
      update((s) => ({ ...s, selectedRunId: runId, entries: [], cursor: null, dropped: 0 }));
      await fetchLogs(true);
    },

    async refreshLogs() {
      await fetchLogs(false);
    },

    async start() {
      update((s) => ({ ...s, logStatus: 'loading' }));
      await Promise.all([this.loadSystem(), this.loadRuns()]);
      await fetchLogs(true);
      this.setLive(get({ subscribe }).live);
    },

    /**
     * Live tailing is a 3s cursor poll, not a websocket.
     *
     * The pane's entire value is TIME CORRELATION across sources - a backend
     * ERROR, the step line 6ms later, the browser's 500. Taking step frames
     * live over the socket while backend records arrive on a 3s poll would
     * interleave them by ARRIVAL rather than by occurrence, and the join the
     * user came here to make would be wrong for up to 3 seconds either way.
     * One cursor over one endpoint gives one consistent order. A websocket
     * upgrade is worth doing later, but it has to carry every source at once.
     */
    setLive(live: boolean) {
      update((s) => ({ ...s, live }));
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      if (!live) return;
      pollTimer = setInterval(() => {
        // A hidden tab that keeps polling is a background load on a machine
        // the user is trying to debug.
        if (typeof document !== 'undefined' && document.hidden) return;
        const s = get({ subscribe });
        if (s.logStatus === 'unavailable') return;
        void fetchLogs(false);
      }, POLL_INTERVAL_MS);
    },

    /**
     * Append a client-side record (source J - the browser half).
     *
     * These arrive live, so appending at arrival time is also chronological
     * order. They carry `seq: null` and never advance the server cursor.
     *
     * Bodies are never captured, here or on the server: a request body on
     * this platform is a card description or a prompt, which is the user's
     * private product thinking and has no place in a public issue.
     */
    noteBrowserEvent(level: string, message: string) {
      update((s) =>
        trim({
          ...s,
          entries: [
            ...s.entries,
            {
              id: `br:${localSeq++}`,
              seq: null,
              ts: new Date().toISOString(),
              source: 'browser',
              kind: 'browser' as const,
              level,
              message,
            },
          ],
        }),
      );
    },

    /**
     * The warm path. `client.ts` throws a structured `ApiError` from one seam
     * for every failed call in the app; wiring that seam to this method turns
     * "something went wrong" anywhere in the UI into a line in this stream
     * with the status and the path attached.
     */
    noteApiError(method: string, path: string, status: number, message: string) {
      this.noteBrowserEvent(
        'ERROR',
        `${method} ${path} -> ${status === NETWORK_ERROR_STATUS ? 'unreachable' : status} ${message}`,
      );
    },

    stop() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    },

    reset() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      localSeq = 0;
      trimHeld = false;
      set(initialState());
    },
  };
}

export const diagnosticsStore = createDiagnosticsStore();

/**
 * True when this backend has no diagnostics API at all.
 *
 * Both halves are checked because they fail independently: a backend can
 * serve `/system` and 404 `/logs` mid-rollout, and the page should say which.
 */
export const diagnosticsUnavailable: Readable<boolean> = derived(
  diagnosticsStore,
  ($d) => $d.systemStatus === 'unavailable' && $d.logStatus === 'unavailable',
);

/**
 * The one-line verdict for the system strip.
 *
 * `unknown` is a real answer and is rendered as one. It is NOT folded into
 * `ok`: "we could not tell whether the running code is current" and "the
 * running code is current" are opposite pieces of news.
 */
export type SystemVerdict = 'ok' | 'stale' | 'unknown' | 'error';

export const systemVerdict: Readable<SystemVerdict> = derived(diagnosticsStore, ($d) => {
  if ($d.systemStatus === 'unavailable' || $d.systemStatus === 'idle') return 'unknown';
  if ($d.systemStatus === 'error') return 'error';
  if (!$d.system) return 'unknown';
  if ($d.system.staleness.stale) return 'stale';
  if (!$d.system.migrations.ok) return 'stale';
  return 'ok';
});

/** The oldest LazyAF container, which is usually the one serving stale code. */
export function oldestContainer(containers: ContainerFacts[]): ContainerFacts | null {
  let oldest: ContainerFacts | null = null;
  for (const c of containers) {
    if (!oldest || c.started_at < oldest.started_at) oldest = c;
  }
  return oldest;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
