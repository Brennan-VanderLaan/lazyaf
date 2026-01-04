import { writable, derived } from 'svelte/store';

export interface DebugBreakpointEvent {
  session_id: string;
  step_index: number;
  step_name: string;
  status: string;
}

export interface DebugStatusEvent {
  session_id: string;
  status: string;
  mode?: string;
}

export interface DebugResumeEvent {
  session_id: string;
  pipeline_run_id: string;
  continue_from_step: number;
}

export interface ActiveDebugSession {
  sessionId: string;
  stepIndex: number | null;
  stepName: string | null;
  status: string;
  mode: string | null;
  pipelineRunId: string | null;
}

function createDebugStore() {
  const sessions = writable<Map<string, ActiveDebugSession>>(new Map());

  function handleBreakpoint(event: DebugBreakpointEvent) {
    sessions.update(map => {
      const existing = map.get(event.session_id);
      map.set(event.session_id, {
        sessionId: event.session_id,
        stepIndex: event.step_index,
        stepName: event.step_name,
        status: event.status,
        mode: existing?.mode || null,
        pipelineRunId: existing?.pipelineRunId || null,
      });
      return new Map(map);
    });
  }

  function handleStatus(event: DebugStatusEvent) {
    sessions.update(map => {
      const existing = map.get(event.session_id);
      if (existing) {
        existing.status = event.status;
        if (event.mode) {
          existing.mode = event.mode;
        }
        map.set(event.session_id, { ...existing });
      } else {
        map.set(event.session_id, {
          sessionId: event.session_id,
          stepIndex: null,
          stepName: null,
          status: event.status,
          mode: event.mode || null,
          pipelineRunId: null,
        });
      }
      return new Map(map);
    });
  }

  function handleResume(event: DebugResumeEvent) {
    sessions.update(map => {
      const existing = map.get(event.session_id);
      if (existing) {
        existing.status = 'resumed';
        existing.pipelineRunId = event.pipeline_run_id;
        map.set(event.session_id, { ...existing });
      }
      return new Map(map);
    });
  }

  function setSession(sessionId: string, pipelineRunId: string) {
    sessions.update(map => {
      map.set(sessionId, {
        sessionId,
        stepIndex: null,
        stepName: null,
        status: 'pending',
        mode: null,
        pipelineRunId,
      });
      return new Map(map);
    });
  }

  function removeSession(sessionId: string) {
    sessions.update(map => {
      map.delete(sessionId);
      return new Map(map);
    });
  }

  function getSession(sessionId: string): ActiveDebugSession | undefined {
    let result: ActiveDebugSession | undefined;
    sessions.subscribe(map => {
      result = map.get(sessionId);
    })();
    return result;
  }

  return {
    subscribe: sessions.subscribe,
    handleBreakpoint,
    handleStatus,
    handleResume,
    setSession,
    removeSession,
    getSession,
  };
}

export const debugStore = createDebugStore();

// Derived store for getting sessions by pipeline run ID
export const debugSessionByRunId = derived(debugStore, ($sessions) => {
  return (runId: string): ActiveDebugSession | undefined => {
    const sessionsArray = Array.from($sessions.values());
    for (let i = 0; i < sessionsArray.length; i++) {
      const session = sessionsArray[i];
      if (session.pipelineRunId === runId) {
        return session;
      }
    }
    return undefined;
  };
});
