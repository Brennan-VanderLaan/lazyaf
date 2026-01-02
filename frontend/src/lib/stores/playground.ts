import { writable, derived, get } from 'svelte/store';
import type { PlaygroundTestRequest, PlaygroundResult, PlaygroundLogEvent, PlaygroundStatus, AgentModel } from '../api/types';
import { playground as playgroundApi } from '../api/client';

interface PlaygroundState {
  // Configuration
  repoId: string | null;
  agentId: string | null;
  repoAgentName: string | null;
  runnerType: 'claude-code' | 'gemini';
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

  // Timing
  startedAt: Date | null;
  completedAt: Date | null;
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
  startedAt: null,
  completedAt: null,
};

function createPlaygroundStore() {
  const { subscribe, set, update } = writable<PlaygroundState>(initialState);

  let eventSource: EventSource | null = null;
  let logBuffer: string[] = [];
  let flushScheduled = false;

  function flushLogs() {
    if (logBuffer.length === 0) return;
    const logsToAdd = logBuffer;
    logBuffer = [];
    flushScheduled = false;
    update((state) => ({
      ...state,
      logs: [...state.logs, ...logsToAdd],
    }));
  }

  function scheduleFlush() {
    if (!flushScheduled) {
      flushScheduled = true;
      requestAnimationFrame(flushLogs);
    }
  }

  function connectSSE(sessionId: string) {
    if (eventSource) {
      eventSource.close();
    }

    // Reset buffer
    logBuffer = [];
    flushScheduled = false;

    const url = playgroundApi.streamUrl(sessionId);
    eventSource = new EventSource(url);

    eventSource.addEventListener('log', (event) => {
      try {
        const data = JSON.parse(event.data);
        // Buffer logs and batch updates
        logBuffer.push(data.data);
        scheduleFlush();
      } catch {
        // Ignore parse errors
      }
    });

    // Handle batch of existing logs (sent on connect)
    eventSource.addEventListener('logs_batch', (event) => {
      try {
        const data = JSON.parse(event.data);
        if (Array.isArray(data.data)) {
          update((state) => ({
            ...state,
            logs: [...state.logs, ...data.data],
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
      console.log('[playground] SSE complete event received:', event.data);
      try {
        const data = JSON.parse(event.data);
        // Fetch final result
        const currentState = get({ subscribe });
        console.log('[playground] Fetching result for session:', currentState.sessionId);
        if (currentState.sessionId) {
          try {
            const result = await playgroundApi.result(currentState.sessionId);
            console.log('[playground] Result fetched:', result);
            update((state) => ({
              ...state,
              status: result.status as PlaygroundStatus,
              diff: result.diff,
              filesChanged: result.files_changed,
              error: result.error,
              completedAt: new Date(),
            }));
          } catch (e) {
            console.error('[playground] Failed to fetch result:', e);
            update((state) => ({
              ...state,
              status: data.data as PlaygroundStatus,
              completedAt: new Date(),
            }));
          }
        }
      } catch (e) {
        console.error('[playground] Failed to parse complete event:', e);
      }

      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
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

    async startTest() {
      const currentState = get({ subscribe });

      if (!currentState.repoId || !currentState.branch) {
        throw new Error('Repository and branch are required');
      }

      // Reset state
      update((state) => ({
        ...state,
        status: 'queued',
        sessionId: null,
        logs: [],
        diff: null,
        filesChanged: [],
        error: null,
        startedAt: new Date(),
        completedAt: null,
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
        }));

        // Connect to SSE stream
        connectSSE(response.session_id);
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

    async cancel() {
      const currentState = get({ subscribe });

      if (!currentState.sessionId) return;

      update((state) => ({ ...state, status: 'cancelled' }));

      try {
        await playgroundApi.cancel(currentState.sessionId);
      } catch {
        // Ignore cancel errors
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
    },

    reset() {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      logBuffer = [];
      flushScheduled = false;
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

export const hasResult = derived(
  playgroundStore,
  ($state) =>
    ($state.status === 'completed' || $state.status === 'failed') &&
    ($state.diff !== null || $state.error !== null || $state.filesChanged.length > 0)
);
