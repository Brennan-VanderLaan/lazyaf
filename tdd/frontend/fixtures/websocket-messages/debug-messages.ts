/**
 * WebSocket message fixtures for debug re-run tests (Phase 12.7).
 *
 * These represent the debug-related messages sent by the backend
 * for breakpoint-based debugging of pipeline runs.
 */

/**
 * Debug session status values
 */
export type DebugSessionStatus =
  | 'pending'
  | 'starting'
  | 'running'
  | 'paused'
  | 'resumed'
  | 'completed'
  | 'aborted'
  | 'failed';

/**
 * Debug mode (how the session was started)
 */
export type DebugMode = 'breakpoint' | 'step-through' | 'continue';

/**
 * debug_breakpoint WebSocket message payload
 * Sent when execution pauses at a breakpoint
 */
export interface DebugBreakpointPayload {
  session_id: string;
  step_index: number;
  step_name: string;
  status: string;
}

/**
 * debug_status WebSocket message payload
 * Sent for session lifecycle updates
 */
export interface DebugStatusPayload {
  session_id: string;
  status: DebugSessionStatus;
  mode?: DebugMode;
}

/**
 * debug_resume WebSocket message payload
 * Sent when user resumes from a breakpoint
 */
export interface DebugResumePayload {
  session_id: string;
  pipeline_run_id: string;
  continue_from_step: number;
}

/**
 * Sample debug session for testing
 */
export const testDebugSession = {
  session_id: 'debug-session-001',
  pipeline_run_id: 'run-test-001',
  pipeline_id: 'pipeline-test-001',
};

/**
 * Sample pipeline steps for breakpoint testing
 */
export const testPipelineSteps = [
  { index: 0, name: 'Clone Repository' },
  { index: 1, name: 'Install Dependencies' },
  { index: 2, name: 'Run Tests' },
  { index: 3, name: 'Build' },
  { index: 4, name: 'Deploy' },
];

/**
 * Pre-built debug_status messages for each status
 */
export const debugStatusMessages = {
  pending: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'pending' as DebugSessionStatus,
    },
  },

  starting: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'starting' as DebugSessionStatus,
      mode: 'breakpoint' as DebugMode,
    },
  },

  running: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'running' as DebugSessionStatus,
      mode: 'breakpoint' as DebugMode,
    },
  },

  paused: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'paused' as DebugSessionStatus,
      mode: 'breakpoint' as DebugMode,
    },
  },

  resumed: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'resumed' as DebugSessionStatus,
    },
  },

  completed: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'completed' as DebugSessionStatus,
    },
  },

  aborted: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'aborted' as DebugSessionStatus,
    },
  },

  failed: {
    type: 'debug_status' as const,
    payload: {
      session_id: testDebugSession.session_id,
      status: 'failed' as DebugSessionStatus,
    },
  },
};

/**
 * Pre-built debug_breakpoint messages for each step
 */
export const debugBreakpointMessages = {
  step0: {
    type: 'debug_breakpoint' as const,
    payload: {
      session_id: testDebugSession.session_id,
      step_index: 0,
      step_name: testPipelineSteps[0].name,
      status: 'paused',
    },
  },

  step1: {
    type: 'debug_breakpoint' as const,
    payload: {
      session_id: testDebugSession.session_id,
      step_index: 1,
      step_name: testPipelineSteps[1].name,
      status: 'paused',
    },
  },

  step2: {
    type: 'debug_breakpoint' as const,
    payload: {
      session_id: testDebugSession.session_id,
      step_index: 2,
      step_name: testPipelineSteps[2].name,
      status: 'paused',
    },
  },

  step3: {
    type: 'debug_breakpoint' as const,
    payload: {
      session_id: testDebugSession.session_id,
      step_index: 3,
      step_name: testPipelineSteps[3].name,
      status: 'paused',
    },
  },

  step4: {
    type: 'debug_breakpoint' as const,
    payload: {
      session_id: testDebugSession.session_id,
      step_index: 4,
      step_name: testPipelineSteps[4].name,
      status: 'paused',
    },
  },
};

/**
 * Pre-built debug_resume messages
 */
export const debugResumeMessages = {
  fromStep0: {
    type: 'debug_resume' as const,
    payload: {
      session_id: testDebugSession.session_id,
      pipeline_run_id: testDebugSession.pipeline_run_id,
      continue_from_step: 0,
    },
  },

  fromStep1: {
    type: 'debug_resume' as const,
    payload: {
      session_id: testDebugSession.session_id,
      pipeline_run_id: testDebugSession.pipeline_run_id,
      continue_from_step: 1,
    },
  },

  fromStep2: {
    type: 'debug_resume' as const,
    payload: {
      session_id: testDebugSession.session_id,
      pipeline_run_id: testDebugSession.pipeline_run_id,
      continue_from_step: 2,
    },
  },
};

/**
 * Complete debug session sequence: start -> breakpoint -> resume -> complete
 */
export const successfulDebugSequence = [
  debugStatusMessages.pending,
  debugStatusMessages.starting,
  debugStatusMessages.running,
  debugBreakpointMessages.step2, // Hit breakpoint at step 2
  debugStatusMessages.paused,
  // User inspects state...
  debugResumeMessages.fromStep2,
  debugStatusMessages.resumed,
  debugStatusMessages.running,
  debugStatusMessages.completed,
];

/**
 * Debug session with multiple breakpoints
 */
export const multiBreakpointSequence = [
  debugStatusMessages.pending,
  debugStatusMessages.starting,
  debugStatusMessages.running,
  debugBreakpointMessages.step1, // First breakpoint
  debugStatusMessages.paused,
  debugResumeMessages.fromStep1,
  debugStatusMessages.resumed,
  debugStatusMessages.running,
  debugBreakpointMessages.step3, // Second breakpoint
  debugStatusMessages.paused,
  debugResumeMessages.fromStep2,
  debugStatusMessages.resumed,
  debugStatusMessages.running,
  debugStatusMessages.completed,
];

/**
 * Debug session aborted by user
 */
export const abortedDebugSequence = [
  debugStatusMessages.pending,
  debugStatusMessages.starting,
  debugStatusMessages.running,
  debugBreakpointMessages.step2,
  debugStatusMessages.paused,
  // User clicks abort...
  debugStatusMessages.aborted,
];

/**
 * Debug session fails during execution
 */
export const failedDebugSequence = [
  debugStatusMessages.pending,
  debugStatusMessages.starting,
  debugStatusMessages.running,
  debugStatusMessages.failed,
];

/**
 * Helper to create a breakpoint event at any step
 */
export function createBreakpointEvent(stepIndex: number, stepName: string) {
  return {
    type: 'debug_breakpoint' as const,
    payload: {
      session_id: testDebugSession.session_id,
      step_index: stepIndex,
      step_name: stepName,
      status: 'paused',
    },
  };
}

/**
 * Helper to create a resume event from any step
 */
export function createResumeEvent(continueFromStep: number) {
  return {
    type: 'debug_resume' as const,
    payload: {
      session_id: testDebugSession.session_id,
      pipeline_run_id: testDebugSession.pipeline_run_id,
      continue_from_step: continueFromStep,
    },
  };
}

/**
 * Helper to create a status event with custom session ID
 */
export function createStatusEvent(
  sessionId: string,
  status: DebugSessionStatus,
  mode?: DebugMode
) {
  return {
    type: 'debug_status' as const,
    payload: {
      session_id: sessionId,
      status,
      ...(mode && { mode }),
    },
  };
}
