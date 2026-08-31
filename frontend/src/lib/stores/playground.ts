import { writable, derived, get } from 'svelte/store';
import type {
  PlaygroundTestRequest,
  PlaygroundResult,
  PlaygroundLogEvent,
  PlaygroundStatus,
  AgentModel,
  ModalityName,
  ModalityState,
  ModelEndpoint,
} from '../api/types';
import { playground as playgroundApi } from '../api/client';
// Lane B owns the modality vocabulary and every sentence it renders. This
// module CONSUMES those; it does not restate them. A second phrasing of
// "never probed" living here is a second thing that has to stay true (R3),
// and the whole point of the six states is that they are not paraphrasable.
import {
  HARNESS_AGENT,
  MODALITIES_UNREPORTED,
  modalitiesReported,
  modalityCells,
} from './endpoints';

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
  /**
   * The playground's own limits and modality answers.
   *
   * Read rather than hardcoded so the attach hint states the numbers the
   * validator actually enforces - see `PlaygroundAttachmentLimits`.
   */
  capabilities: () => apiGet<PlaygroundCapabilities>('/playground/capabilities'),
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

  // What the PLATFORM can carry (14.5). `null` until the read lands, and a
  // null here DISABLES the attach control rather than defaulting it open:
  // "we could not ask" is not "yes".
  capabilities: PlaygroundCapabilities | null;
  capabilitiesError: string | null;

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
  capabilities: null,
  capabilitiesError: null,
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

    /**
     * Read the playground's own limits and modality answers, once.
     *
     * Idempotent by design: it is a property of the BUILD, not of a run, so
     * calling it twice is a wasted round trip rather than a correctness
     * problem, and a page that re-reads it on every mount is fine.
     *
     * A FAILURE IS RECORDED, NOT SWALLOWED. `capabilities` stays null and
     * `capabilitiesError` carries the server's own words; `attachmentGate`
     * then disables the attach control and says the read failed. Defaulting to
     * a permissive answer here is the exact shape of bug this feature exists
     * to prevent - a file accepted because nobody could confirm it would be
     * carried.
     */
    async loadCapabilities(): Promise<void> {
      if (get({ subscribe }).capabilities) return;
      try {
        const capabilities = await playgroundHistoryApi.capabilities();
        if (!isCapabilityPayload(capabilities)) {
          // A 200 carrying the wrong shape is refused rather than stored.
          // Writing it would put `undefined` where the template reads
          // `.modalities.find(...)`, and a page that throws mid-render leaves
          // the attach control with no state at all - which is worse than
          // either answer it could have given.
          throw new Error(
            'the playground capability read returned an unrecognised shape',
          );
        }
        update((state) => ({ ...state, capabilities, capabilitiesError: null }));
      } catch (e) {
        update((state) => ({
          ...state,
          capabilities: null,
          capabilitiesError: e instanceof Error ? e.message : String(e),
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
      // Genuinely back to the initial state, with ONE carried field. The
      // page's own reactive block notices `repoId` no longer matches the
      // selected repo and re-seeds it (and re-loads history) on the next tick,
      // so Reset clears the RUN without this method having to keep a special
      // exception list.
      //
      // `capabilities` is carried because it is a property of the BUILD, not
      // of the run: dropping it would make the attach hint stop stating its
      // limits until a second round trip landed, and a control that briefly
      // cannot say why it is disabled is the thing this feature exists to
      // avoid. Nothing about it can be stale within one page load.
      const { capabilities, capabilitiesError } = get({ subscribe });
      set({ ...initialState, capabilities, capabilitiesError });
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
// ===========================================================================
// Attachments - what the PLATFORM can carry (Milestone 14.5)
// ===========================================================================

/**
 * The playground's own limits and modality answers, straight off
 * `GET /api/playground/capabilities`.
 *
 * FETCHED RATHER THAN RE-SPELLED. The caps live in
 * `backend/app/schemas/playground.py` because that is where they are enforced;
 * a "max 5 MiB" typed into a Svelte template beside a `5 * 1024 * 1024` in a
 * validator is two sources of truth for one contract (R3), and the half that
 * drifts is always the sentence a human reads. Rendering the server's numbers
 * means the copy is wrong only if the validator is.
 */
export interface PlaygroundAttachmentLimits {
  max_files: number;
  max_bytes_per_file: number;
  max_bytes_total: number;
  media_types: string[];
}

/**
 * Whether the PLATFORM can carry one modality - deliberately not a statement
 * about any endpoint.
 *
 * Two different facts have to both be true before a human can attach
 * anything: this endpoint accepted an image content part (a probe result,
 * `EndpointCapabilities.modalities`), and LazyAF has somewhere to put one (the
 * harness transcript). Collapsing them would make "your endpoint cannot see"
 * and "LazyAF cannot send" the same sentence, when they call for opposite
 * actions - probe the endpoint, versus wait for the plumbing.
 */
export interface PlaygroundModalitySupport {
  modality: string;
  attachable: boolean;
  /** Populated in BOTH states. A greyed control always says why. */
  reason: string;
}

export interface PlaygroundCapabilities {
  attachment_limits: PlaygroundAttachmentLimits;
  modalities: PlaygroundModalitySupport[];
}

/**
 * Does this payload actually carry both halves of the capability contract?
 *
 * Checked because a 200 is not a promise about shape. A dev proxy, a
 * catch-all route or a backend one version behind can all answer this path
 * with something that is JSON and is not this - and storing it would put
 * `undefined` where the template reads `.modalities.find(...)`. A page that
 * throws mid-render leaves the attach control with no state at all, which is
 * worse than either answer it could have given.
 */
export function isCapabilityPayload(value: unknown): value is PlaygroundCapabilities {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const candidate = value as Partial<PlaygroundCapabilities>;
  const limits = candidate.attachment_limits;
  return (
    Array.isArray(candidate.modalities) &&
    !!limits &&
    typeof limits === 'object' &&
    typeof limits.max_files === 'number' &&
    typeof limits.max_bytes_per_file === 'number' &&
    typeof limits.max_bytes_total === 'number' &&
    Array.isArray(limits.media_types)
  );
}

/** Render a byte count the way the attach hint states a limit. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return 'unknown';
  const mib = bytes / (1024 * 1024);
  if (mib >= 1) return `${Number.isInteger(mib) ? mib : mib.toFixed(1)} MiB`;
  const kib = bytes / 1024;
  return `${Number.isInteger(kib) ? kib : kib.toFixed(1)} KiB`;
}

/** One sentence stating every cap, built from the SERVER's numbers. */
export function limitsSentence(limits: PlaygroundAttachmentLimits | null): string {
  if (!limits) return 'Limits unknown - the playground capability read failed.';
  const kinds = limits.media_types
    .map((t) => t.replace(/^image\//, '').toUpperCase())
    .join(', ');
  return (
    `Up to ${limits.max_files} files, ${formatBytes(limits.max_bytes_per_file)} each ` +
    `and ${formatBytes(limits.max_bytes_total)} in total. ${kinds}. ` +
    `Anything over is refused at the edge, before a container starts.`
  );
}

/** Which link in the chain said no. Also the control's `data-blocked-by`. */
export type AttachBlocker =
  | 'runner'
  | 'no-endpoint'
  | 'unreported'
  | 'endpoint'
  | 'platform'
  | null;

export interface AttachmentGate {
  /** True only when a human could attach this RIGHT NOW. */
  enabled: boolean;
  blockedBy: AttachBlocker;
  /** Why, in one sentence a human can act on. NEVER empty. */
  reason: string;
  /** The next thing to DO, when there is one ("Probe this endpoint."). */
  next: string | null;
  /** The endpoint's own six-state answer for this modality, when it has one. */
  state: ModalityState | null;
}

/**
 * Whether this configuration may attach one modality, and why not when it may
 * not.
 *
 * THE ORDER OF THE CHECKS IS THE DESIGN, because each one sends the human
 * somewhere different and only the first one is shown:
 *
 *   runner       Claude Code and Gemini are CLI agents. Their file handling
 *                belongs to the CLI, not to an endpoint row, so "this endpoint
 *                cannot see images" would be answering a question nobody asked.
 *   no-endpoint  Nothing to say yet. Pick one.
 *   unreported   The backend has no `modalities` list at all. NOT "unprobed":
 *                probing cannot fix it, and offering Probe here sends someone
 *                round a loop that never terminates.
 *   endpoint     The endpoint's own answer, in Lane B's words - not
 *                paraphrased here, because a second paraphrase is a second
 *                thing to keep true (R3). This is the check the brief is
 *                really about: `unprobed` and `undetectable` are DISABLED, and
 *                each says its own sentence, because "we never asked" and "it
 *                took the image and dropped it" lead to different actions.
 *   platform     The endpoint says yes and LazyAF still cannot carry it. Last
 *                on purpose: it is the same answer for every endpoint, so
 *                showing it to someone whose endpoint was never probed would
 *                point them at the wrong fix.
 *
 * FAIL CLOSED at every unknown. A missing capability read, a missing modality
 * list and a missing cell all DISABLE. The failure this whole feature exists
 * to prevent is a file that is accepted and never reaches the model, and an
 * optimistic default is exactly how that ships.
 */
export function attachmentGate(params: {
  runnerType: string;
  endpoint: ModelEndpoint | null | undefined;
  platform: PlaygroundModalitySupport | null | undefined;
  modality?: ModalityName;
}): AttachmentGate {
  const modality = params.modality ?? 'images';

  if (params.runnerType !== HARNESS_AGENT) {
    return {
      enabled: false,
      blockedBy: 'runner',
      reason:
        'Claude Code and Gemini are CLI agents, not endpoints. What they can read is a property of the CLI and of the files in the workspace, not of a model endpoint row - so this control does not apply, and their blank capability strip is not a claim that they cannot see.',
      next: 'Switch the runner to "Self-hosted endpoint" to attach here.',
      state: null,
    };
  }

  const endpoint = params.endpoint ?? null;
  if (!endpoint) {
    return {
      enabled: false,
      blockedBy: 'no-endpoint',
      reason: 'No endpoint selected, so nothing has been asked about images yet.',
      next: 'Pick a self-hosted endpoint above.',
      state: null,
    };
  }

  if (!modalitiesReported(endpoint)) {
    return {
      enabled: false,
      blockedBy: 'unreported',
      reason: MODALITIES_UNREPORTED,
      next: null,
      state: null,
    };
  }

  const cell = modalityCells(endpoint).find((c) => c.key === modality) ?? null;
  if (!cell) {
    return {
      enabled: false,
      blockedBy: 'unreported',
      reason: `This backend reported modality detection but said nothing about ${modality}. That is an absence, not a "no".`,
      next: null,
      state: null,
    };
  }

  if (cell.state !== 'supported') {
    return {
      enabled: false,
      blockedBy: 'endpoint',
      reason: cell.detail,
      next: cell.next,
      state: cell.state as ModalityState,
    };
  }

  const platform = params.platform ?? null;
  if (!platform) {
    return {
      enabled: false,
      blockedBy: 'platform',
      reason:
        "The playground's own capability read failed, so whether LazyAF can carry this file is unknown. Unknown is not yes.",
      next: 'Reload the page.',
      state: 'supported',
    };
  }
  if (!platform.attachable) {
    return {
      enabled: false,
      blockedBy: 'platform',
      reason: platform.reason,
      next: null,
      state: 'supported',
    };
  }

  return {
    enabled: true,
    blockedBy: null,
    reason: `${endpoint.name} accepted an image content part when it was probed.`,
    next: null,
    state: 'supported',
  };
}
