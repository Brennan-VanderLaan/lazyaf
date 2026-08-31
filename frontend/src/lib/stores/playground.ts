import { writable, derived, get } from 'svelte/store';
import type { PlaygroundTestRequest, PlaygroundResult, PlaygroundLogEvent, PlaygroundStatus, AgentModel } from '../api/types';
import { playground as playgroundApi } from '../api/client';

/**
 * Where a result's facts came from. Mirrors `PlaygroundResult.source` /
 * `PlaygroundStatus.source` on the wire (backend/app/schemas/playground.py).
 *
 * `run` means the in-memory session was swept (30-minute TTL, or a backend
 * restart) and the transcript was rebuilt from the durable PipelineRun. That
 * record cannot carry the DIFF - the `playground/<id>` branch is deleted once
 * the diff has been computed - so the UI has to say so rather than render the
 * indistinguishable "no changes were made" (R1).
 */
export type ResultSource = 'session' | 'run';

/** One past playground run. Mirrors `PlaygroundSessionSummary` on the wire. */
export interface PlaygroundSessionSummary {
  session_id: string;
  run_id: string;
  status: PlaygroundStatus;
  prompt: string;
  agent: string | null;
  model: string | null;
  base_branch: string | null;
  work_branch: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  live: boolean;
}

/**
 * The playground history/reattach calls live here rather than in
 * `api/client.ts` for one reason worth writing down: this QA lane owns the
 * playground files exclusively and `client.ts` is being edited concurrently
 * by other lanes. They use the same `/api` base and the same "throw with the
 * server's own detail" shape as `client.ts` so nothing here fails silently.
 *
 * HANDOFF: fold these two into the `playground` object in `api/client.ts`
 * once the concurrent edits there have landed - one module per wire contract
 * is the house rule (R3), and this is the only playground call outside it.
 */
const API_BASE = '/api';

async function apiGet<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`);
  } catch (e) {
    throw new Error(
      `could not reach the backend for ${path}: ${e instanceof Error ? e.message : e}`
    );
  }
  if (!response.ok) {
    // Surface the server's own words. A history panel that silently renders
    // empty when the endpoint is broken is the dark failure R1 forbids.
    const body = await response.text().catch(() => '');
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
    } catch {
      // Non-JSON body: keep the raw text, which is more use than "error".
    }
    throw new Error(
      `GET ${path} failed (${response.status})${detail ? `: ${detail}` : ''}`
    );
  }
  return (await response.json()) as T;
}

export const playgroundHistoryApi = {
  list: (repoId: string, limit = 20) =>
    apiGet<PlaygroundSessionSummary[]>(
      `/repos/${repoId}/playground/sessions?limit=${limit}`
    ),
  status: (sessionId: string) =>
    apiGet<{
      session_id: string;
      status: PlaygroundStatus;
      started_at: string | null;
      completed_at: string | null;
      source: ResultSource;
    }>(`/playground/${sessionId}/status`),
  result: (sessionId: string) =>
    apiGet<PlaygroundResult & { source: ResultSource }>(
      `/playground/${sessionId}/result`
    ),
};

/**
 * The session id of the run this tab is looking at.
 *
 * Kept in sessionStorage so a RELOAD lands back on that run - which matters
 * twice over. A reload mid-run used to orphan a live agent container that
 * nobody was watching and nobody could now cancel; a reload after a run used
 * to throw away a transcript that was sitting in the database the whole time.
 * The id is therefore kept until Reset, not cleared on completion.
 *
 * sessionStorage rather than localStorage on purpose: a run belongs to the tab
 * that started it, and two tabs silently fighting over one session's Cancel
 * button is a worse bug than the one being fixed.
 *
 * Every access is guarded: storage throws outright in some privacy modes, and
 * a playground that cannot render because a storage read failed is a strictly
 * worse product than one that forgets a session id.
 */
const SESSION_KEY = 'lazyaf.playground.sessionId';

/**
 * Every status a playground session is allowed to be in.
 *
 * Used to refuse anything else that comes back over the wire rather than
 * writing it into the store, where an unrecognised value renders as a page
 * with no badge, no buttons and no explanation.
 */
const TERMINAL_OR_LIVE = new Set([
  'queued',
  'running',
  'completed',
  'failed',
  'cancelled',
]);

function rememberSession(sessionId: string | null): void {
  try {
    if (sessionId) {
      sessionStorage.setItem(SESSION_KEY, sessionId);
    } else {
      sessionStorage.removeItem(SESSION_KEY);
    }
  } catch {
    // Storage unavailable (private mode, blocked site data). Reattach is a
    // convenience; losing it must never break the page.
  }
}

function recallSession(): string | null {
  try {
    return sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

/**
 * Strip ANSI SGR/CSI sequences from a log line.
 *
 * A real claude-code or pytest run emits colour codes; the pane renders plain
 * text, so without this the user reads `ESC[32mPASS ESC[0m tests/x.py` with
 * unprintable boxes in it. Stripped at the STORE boundary, not in the
 * template, so the Copy button puts clean text on the clipboard too.
 */
const ANSI_PATTERN =
  // eslint-disable-next-line no-control-regex -- matching control chars is the point
  /[\u001B\u009B][[\]()#;?]*(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-PR-TZcf-ntqry=><~]/g;

export function stripAnsi(line: string): string {
  return line.replace(ANSI_PATTERN, '');
}

interface PlaygroundState {
  // Configuration
  repoId: string | null;
  agentId: string | null;
  repoAgentName: string | null;
  // M14 adds 'openai-harness': the LazyAF loop against a self-hosted endpoint.
  runnerType: 'claude-code' | 'gemini' | 'openai-harness';
  model: AgentModel | null;  // Specific model to use
  branch: string | null;
  taskOverride: string;
  saveToBranch: boolean;
  saveBranchName: string;

  // Execution state
  status: PlaygroundStatus;
  sessionId: string | null;
  logs: string[];
  diff: string | null;
  filesChanged: string[];
  error: string | null;
  /** Where `diff` / `logs` came from. See ResultSource. */
  resultSource: ResultSource;
  /** The prompt this run was started with, kept for display after the run. */
  ranPrompt: string | null;

  // History (past runs of the selected repo)
  history: PlaygroundSessionSummary[];
  historyLoading: boolean;
  historyError: string | null;
  /** session_id of the history row currently being shown, if any. */
  viewingSessionId: string | null;

  // Timing
  startedAt: Date | null;
  completedAt: Date | null;
  /**
   * How long the run took, as the SERVER measured it.
   *
   * Preferred over `completedAt - startedAt` for a finished run, and the only
   * duration a run reopened from history has at all - the client was not
   * watching when it happened, and inventing timestamps to fake one would be
   * a lie the UI then renders with a straight face.
   */
  durationSeconds: number | null;
}

const initialState: PlaygroundState = {
  repoId: null,
  agentId: null,
  repoAgentName: null,
  runnerType: 'claude-code',
  model: 'claude-sonnet-4-5-20250929',  // Default to Sonnet 4.5
  branch: null,
  taskOverride: '',
  saveToBranch: false,
  saveBranchName: '',
  status: 'idle',
  sessionId: null,
  logs: [],
  diff: null,
  filesChanged: [],
  error: null,
  resultSource: 'session',
  ranPrompt: null,
  history: [],
  historyLoading: false,
  historyError: null,
  viewingSessionId: null,
  startedAt: null,
  completedAt: null,
  durationSeconds: null,
};

function createPlaygroundStore() {
  const { subscribe, set, update } = writable<PlaygroundState>(initialState);

  let eventSource: EventSource | null = null;
  let logBuffer: string[] = [];
  let flushScheduled = false;
  let flushFallback: ReturnType<typeof setTimeout> | null = null;

  function flushLogs() {
    if (flushFallback !== null) {
      clearTimeout(flushFallback);
      flushFallback = null;
    }
    flushScheduled = false;
    if (logBuffer.length === 0) return;
    const logsToAdd = logBuffer;
    logBuffer = [];
    update((state) => ({
      ...state,
      logs: [...state.logs, ...logsToAdd],
    }));
  }

  /** Drop anything buffered and disarm both pending flushes. */
  function cancelFlush() {
    logBuffer = [];
    flushScheduled = false;
    if (flushFallback !== null) {
      clearTimeout(flushFallback);
      flushFallback = null;
    }
  }

  function scheduleFlush() {
    if (flushScheduled) return;
    flushScheduled = true;
    // rAF coalesces a burst into one update per paint - but it does NOT fire
    // in a hidden tab. Measured: a run streamed while the tab was backgrounded
    // buffered its entire transcript and put NOTHING in the store, so the
    // completed run looked like it had produced no output at all. The timeout
    // is the floor that guarantees the transcript exists either way.
    requestAnimationFrame(flushLogs);
    flushFallback = setTimeout(flushLogs, 250);
  }

  async function loadResult(sessionId: string, fallbackStatus?: PlaygroundStatus) {
    try {
      const result = await playgroundHistoryApi.result(sessionId);
      update((state) => ({
        ...state,
        status: result.status as PlaygroundStatus,
        diff: result.diff,
        filesChanged: result.files_changed,
        error: result.error,
        resultSource: result.source ?? 'session',
        durationSeconds: result.duration_seconds ?? state.durationSeconds,
        completedAt: new Date(),
      }));
    } catch (e) {
      // R1: a result we could not fetch is reported, not hidden behind a
      // status that says everything finished cleanly.
      update((state) => ({
        ...state,
        status: fallbackStatus ?? state.status,
        error:
          state.error ??
          `the run finished but its result could not be read: ${
            e instanceof Error ? e.message : e
          }`,
        completedAt: new Date(),
      }));
    }
  }

  function connectSSE(sessionId: string) {
    if (eventSource) {
      eventSource.close();
    }

    // Reset buffer
    cancelFlush();

    const url = playgroundApi.streamUrl(sessionId);
    eventSource = new EventSource(url);

    eventSource.addEventListener('log', (event) => {
      try {
        const data = JSON.parse(event.data);
        // Buffer logs and batch updates
        logBuffer.push(stripAnsi(data.data));
        scheduleFlush();
      } catch {
        // Ignore parse errors
      }
    });

    // Handle batch of existing logs (sent on connect).
    //
    // This is a full SNAPSHOT of the session's transcript, sent on EVERY
    // connect - including the automatic reconnect EventSource performs after
    // a dropped connection. Appending it duplicated the entire history on
    // every reconnect (two reconnects, three copies). It REPLACES.
    eventSource.addEventListener('logs_batch', (event) => {
      try {
        const data = JSON.parse(event.data);
        if (Array.isArray(data.data)) {
          // Anything buffered from the previous connection belongs to the
          // snapshot we are about to install; dropping it avoids a seam.
          cancelFlush();
          update((state) => ({
            ...state,
            logs: data.data.map((line: string) => stripAnsi(line)),
          }));
        }
      } catch {
        // Ignore parse errors
      }
    });

    eventSource.addEventListener('status', (event) => {
      try {
        const data = JSON.parse(event.data);
        update((state) => ({
          ...state,
          status: data.data as PlaygroundStatus,
          startedAt: data.data === 'running' && !state.startedAt ? new Date() : state.startedAt,
        }));
      } catch {
        // Ignore parse errors
      }
    });

    eventSource.addEventListener('complete', async (event) => {
      let terminalStatus: PlaygroundStatus | undefined;
      try {
        const data = JSON.parse(event.data);
        terminalStatus = data.data as PlaygroundStatus;
      } catch {
        // Malformed complete frame: the result fetch below still lands.
      }

      const currentState = get({ subscribe });
      if (currentState.sessionId) {
        await loadResult(currentState.sessionId, terminalStatus);
      }

      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      // The session id is KEPT on purpose. A finished run is still the thing
      // this tab is looking at, and its transcript is durable server-side, so
      // a reload should come back to it rather than to an empty pane. Only
      // Reset forgets a run.
      void playgroundStore.loadHistory();
    });

    // Handle custom error events from server
    eventSource.addEventListener('error', (event) => {
      try {
        // Server-sent error event with data
        const messageEvent = event as MessageEvent;
        if (messageEvent.data) {
          const data = JSON.parse(messageEvent.data);
          update((state) => ({
            ...state,
            error: data.data || 'Server error',
          }));
        }
      } catch {
        // Ignore parse errors on error events
      }
    });

    // Native SSE error (connection issues)
    eventSource.onerror = () => {
      // Only set error if we're still supposed to be running
      const currentState = get({ subscribe });
      if (currentState.status === 'running' || currentState.status === 'queued') {
        // Connection lost while running - don't immediately error, SSE may reconnect
        // Only log for debugging
        console.warn('SSE connection error, may reconnect...');
      }
    };
  }

  return {
    subscribe,

    setConfig(config: Partial<PlaygroundState>) {
      update((state) => ({ ...state, ...config }));
    },

    /**
     * Re-attach the page to whatever run this tab was watching.
     *
     * Called from the page's onMount, which now fires in three situations
     * that all used to lose the run:
     *
     *   1. A RELOAD mid-run. The session id comes back from sessionStorage,
     *      the stream re-opens, and the Cancel button exists again - before
     *      this, F5 orphaned a live agent container that nobody could stop.
     *   2. Navigating away and back mid-run. The store singleton still holds
     *      the session id but its EventSource was closed on unmount, so the
     *      stream is re-opened rather than left silently dead.
     *   3. A reload after the run finished. The transcript is re-read from
     *      the durable run record.
     */
    async reattach(): Promise<void> {
      const state = get({ subscribe });
      const sessionId = state.sessionId ?? recallSession();
      if (!sessionId) return;

      // Already streaming this very session: nothing to do.
      if (state.sessionId === sessionId && eventSource) return;
      // Finished run whose transcript is already on screen: leave it alone.
      if (
        state.sessionId === sessionId &&
        state.logs.length > 0 &&
        state.status !== 'queued' &&
        state.status !== 'running'
      ) {
        return;
      }

      let status;
      try {
        status = await playgroundHistoryApi.status(sessionId);
      } catch {
        // The session is genuinely gone (a 404 after a test reset, or the
        // backend is down). Forget it rather than nagging about a run that no
        // longer exists - history still lists it if a run record survived.
        rememberSession(null);
        return;
      }

      if (!TERMINAL_OR_LIVE.has(status.status as string)) {
        // R1: a status this client does not recognise is not something to
        // adopt. Writing it would leave the page in a state with no badge, no
        // buttons and no explanation - the silent-wrong shape. Say so and
        // start clean instead.
        console.error(
          `[playground] session ${sessionId} reported an unknown status`,
          status.status,
        );
        rememberSession(null);
        return;
      }

      update((s) => ({
        ...s,
        sessionId,
        status: status.status,
        startedAt: status.started_at ? new Date(status.started_at) : s.startedAt,
        completedAt: status.completed_at ? new Date(status.completed_at) : null,
        viewingSessionId: null,
      }));

      if (status.status === 'queued' || status.status === 'running') {
        // Still in flight: re-open the stream so the transcript keeps
        // arriving AND so the Cancel button comes back. The server replays
        // the whole transcript on connect, so nothing is missed.
        connectSSE(sessionId);
      } else {
        await playgroundStore.openSession(sessionId);
      }
    },

    /** Load a past run's transcript and result into the output pane. */
    async openSession(sessionId: string): Promise<void> {
      // Remembered too: a reload should come back to the run the user is
      // looking at, not to an empty pane.
      rememberSession(sessionId);
      update((state) => ({
        ...state,
        viewingSessionId: sessionId,
        logs: [],
        diff: null,
        filesChanged: [],
        error: null,
      }));
      try {
        const result = await playgroundHistoryApi.result(sessionId);
        update((state) => ({
          ...state,
          sessionId,
          status: result.status as PlaygroundStatus,
          logs: result.logs ? result.logs.split('\n').map(stripAnsi) : [],
          diff: result.diff,
          filesChanged: result.files_changed,
          error: result.error,
          resultSource: result.source ?? 'session',
          durationSeconds: result.duration_seconds ?? null,
          startedAt: null,
          completedAt: null,
        }));
      } catch (e) {
        update((state) => ({
          ...state,
          viewingSessionId: null,
          error: `could not open that run: ${e instanceof Error ? e.message : e}`,
        }));
      }
    },

    async loadHistory(): Promise<void> {
      const repoId = get({ subscribe }).repoId;
      if (!repoId) return;
      update((state) => ({ ...state, historyLoading: true, historyError: null }));
      try {
        const history = await playgroundHistoryApi.list(repoId);
        update((state) => ({
          ...state,
          // Guard against a slow response for a repo the user has left.
          history: state.repoId === repoId ? history : state.history,
          historyLoading: false,
        }));
      } catch (e) {
        update((state) => ({
          ...state,
          historyLoading: false,
          historyError: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    async startTest() {
      const currentState = get({ subscribe });

      if (!currentState.repoId || !currentState.branch) {
        throw new Error('Repository and branch are required');
      }
      if (!currentState.taskOverride.trim()) {
        throw new Error('A task description is required');
      }

      // Reset state.
      //
      // `logs` is NOT cleared here. The previous run's transcript stays on
      // screen until the new session's first frame replaces it, so starting a
      // run never blanks the pane you were reading - and the old run is in
      // history either way now.
      update((state) => ({
        ...state,
        status: 'queued',
        sessionId: null,
        viewingSessionId: null,
        diff: null,
        filesChanged: [],
        error: null,
        resultSource: 'session',
        ranPrompt: state.taskOverride,
        startedAt: new Date(),
        completedAt: null,
        durationSeconds: null,
      }));

      try {
        const request: PlaygroundTestRequest = {
          agent_id: currentState.agentId,
          repo_agent_name: currentState.repoAgentName,
          runner_type: currentState.runnerType,
          model: currentState.model,
          branch: currentState.branch,
          task_override: currentState.taskOverride || null,
          save_to_branch: currentState.saveToBranch ? currentState.saveBranchName : null,
        };

        const response = await playgroundApi.start(currentState.repoId, request);

        update((state) => ({
          ...state,
          sessionId: response.session_id,
          status: response.status as PlaygroundStatus,
          // The new run owns the pane from here.
          logs: [],
        }));
        rememberSession(response.session_id);

        // Connect to SSE stream
        connectSSE(response.session_id);
        void this.loadHistory();
      } catch (e) {
        update((state) => ({
          ...state,
          status: 'failed',
          error: e instanceof Error ? e.message : 'Failed to start test',
          completedAt: new Date(),
        }));
        throw e;
      }
    },

    /**
     * Ask the server to stop the run.
     *
     * A REFUSED cancel is not a cancel. The server raises
     * `PlaygroundCancelError` and answers 503 specifically so that "cancelled"
     * is never reported for a container that is still running and still
     * spending; swallowing that put the user's mind at rest about a thing
     * that had not happened (R1). On a refusal the previous status is
     * restored, the server's own words are shown, and the stream is left open
     * so the retry has something to watch.
     */
    async cancel() {
      const currentState = get({ subscribe });

      if (!currentState.sessionId) return;

      const previousStatus = currentState.status;
      update((state) => ({ ...state, status: 'cancelled', error: null }));

      try {
        await playgroundApi.cancel(currentState.sessionId);
      } catch (e) {
        update((state) => ({
          ...state,
          status: previousStatus,
          error:
            e instanceof Error
              ? `Cancel failed: ${e.message}`
              : 'Cancel failed; the agent container may still be running.',
        }));
        return;
      }

      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }

      update((state) => ({
        ...state,
        status: 'cancelled',
        completedAt: new Date(),
      }));
      void this.loadHistory();
    },

    /**
     * Close the stream without touching the run.
     *
     * Used by the page's onDestroy. It used to CANCEL there, which meant that
     * clicking any nav link killed the agent container mid-run, with no
     * prompt and no undo - almost certainly what "outputs weren't saved" was.
     * Leaving the page now leaves the run alone; the store is a module
     * singleton, so coming back shows it still going.
     */
    detach() {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
    },

    reset() {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      cancelFlush();
      rememberSession(null);
      // Genuinely back to the initial state. The page's own reactive block
      // notices `repoId` no longer matches the selected repo and re-seeds it
      // (and re-loads history) on the next tick, so Reset clears the RUN
      // without this method having to keep a special exception list.
      set(initialState);
    },

    clearLogs() {
      update((state) => ({ ...state, logs: [] }));
    },
  };
}

export const playgroundStore = createPlaygroundStore();

// Derived stores
export const isRunning = derived(
  playgroundStore,
  ($state) => $state.status === 'queued' || $state.status === 'running'
);

export const canStart = derived(
  playgroundStore,
  ($state) =>
    $state.status === 'idle' ||
    $state.status === 'completed' ||
    $state.status === 'failed' ||
    $state.status === 'cancelled'
);

/**
 * "This run reached a terminal state" - NOT "this run produced a diff".
 *
 * It used to also require a diff, an error or a changed file, so the single
 * most natural first prompt a stranger types ("what does this repo do?")
 * finished with no Changes section, no "nothing changed" message and no Reset
 * button: the page looked like nothing had happened.
 */
export const hasResult = derived(
  playgroundStore,
  ($state) =>
    $state.status === 'completed' ||
    $state.status === 'failed' ||
    $state.status === 'cancelled'
);
