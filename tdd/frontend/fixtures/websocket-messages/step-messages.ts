/**
 * WebSocket message fixtures for step execution tests.
 *
 * These represent the new step-related messages added in Phase 12.
 */

/**
 * Step execution states from Phase 12
 */
export type StepExecutionState =
  | 'pending'
  | 'preparing'
  | 'running'
  | 'completing'
  | 'completed'
  | 'failed'
  | 'cancelled';

/**
 * step_status WebSocket message payload
 */
export interface StepStatusPayload {
  runner_id: string;
  step_id: string;
  status: StepExecutionState;
  exit_code?: number;
  error?: string | null;
}

/**
 * step_logs WebSocket message payload
 */
export interface StepLogsPayload {
  runner_id: string;
  step_id: string;
  lines: string[];
}

/**
 * Sample step for testing
 */
export const testStep = {
  id: 'step-test-001',
  name: 'Build and Test',
  runner_id: 'runner-test-001',
};

/**
 * Pre-built step_status messages for each state
 */
export const stepStatusMessages = {
  pending: {
    type: 'step_status' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      status: 'pending' as StepExecutionState,
    },
  },

  preparing: {
    type: 'step_status' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      status: 'preparing' as StepExecutionState,
    },
  },

  running: {
    type: 'step_status' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      status: 'running' as StepExecutionState,
    },
  },

  completing: {
    type: 'step_status' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      status: 'completing' as StepExecutionState,
    },
  },

  completed: {
    type: 'step_status' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      status: 'completed' as StepExecutionState,
      exit_code: 0,
    },
  },

  failed: {
    type: 'step_status' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      status: 'failed' as StepExecutionState,
      exit_code: 1,
      error: 'Step execution failed: non-zero exit code',
    },
  },

  cancelled: {
    type: 'step_status' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      status: 'cancelled' as StepExecutionState,
    },
  },
};

/**
 * Sample log messages
 */
export const stepLogsMessages = {
  startup: {
    type: 'step_logs' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      lines: [
        '[2024-01-15 10:00:00] Starting step execution...',
        '[2024-01-15 10:00:01] Cloning repository...',
      ],
    },
  },

  progress: {
    type: 'step_logs' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      lines: [
        '[2024-01-15 10:00:05] Running tests...',
        '[2024-01-15 10:00:10] Test 1/10 passed',
        '[2024-01-15 10:00:15] Test 2/10 passed',
      ],
    },
  },

  completion: {
    type: 'step_logs' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      lines: [
        '[2024-01-15 10:01:00] All tests passed!',
        '[2024-01-15 10:01:01] Step completed successfully.',
      ],
    },
  },

  error: {
    type: 'step_logs' as const,
    payload: {
      runner_id: testStep.runner_id,
      step_id: testStep.id,
      lines: [
        '[2024-01-15 10:00:30] ERROR: Test 5/10 failed',
        '[2024-01-15 10:00:30] AssertionError: expected 1 to equal 2',
        '[2024-01-15 10:00:31] Step failed with exit code 1',
      ],
    },
  },
};

/**
 * Complete successful step execution sequence
 */
export const successfulStepSequence = [
  stepStatusMessages.pending,
  stepStatusMessages.preparing,
  stepLogsMessages.startup,
  stepStatusMessages.running,
  stepLogsMessages.progress,
  stepLogsMessages.completion,
  stepStatusMessages.completing,
  stepStatusMessages.completed,
];

/**
 * Failed step execution sequence
 */
export const failedStepSequence = [
  stepStatusMessages.pending,
  stepStatusMessages.preparing,
  stepLogsMessages.startup,
  stepStatusMessages.running,
  stepLogsMessages.error,
  stepStatusMessages.failed,
];

/**
 * Cancelled step sequence
 */
export const cancelledStepSequence = [
  stepStatusMessages.pending,
  stepStatusMessages.preparing,
  stepStatusMessages.running,
  stepStatusMessages.cancelled,
];
