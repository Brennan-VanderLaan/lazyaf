/**
 * WebSocket message fixtures for runner-related tests.
 *
 * These represent the actual message shapes sent by the backend
 * after Phase 12 changes. Use these to test frontend WebSocket handling.
 */

/**
 * New runner states from Phase 12.6
 */
export type RunnerState =
  | 'disconnected'
  | 'connecting'
  | 'idle'
  | 'assigned'
  | 'busy'
  | 'dead';

/**
 * Runner status WebSocket message payload
 */
export interface RunnerStatusPayload {
  id: string;
  name?: string;
  status: RunnerState;
  runner_type?: string;
  current_step_id?: string | null;
}

/**
 * Runner registration success message
 */
export interface RunnerRegisteredPayload {
  runner_id: string;
}

/**
 * Sample runner for testing
 */
export const testRunner = {
  id: 'runner-test-001',
  name: 'test-runner-claude',
  runner_type: 'claude-code',
};

/**
 * Pre-built runner_status messages for each state
 */
export const runnerStatusMessages = {
  /**
   * Runner just registered and is ready for work
   */
  idle: {
    type: 'runner_status' as const,
    payload: {
      id: testRunner.id,
      name: testRunner.name,
      status: 'idle' as RunnerState,
      runner_type: testRunner.runner_type,
    },
  },

  /**
   * Runner has been assigned a step, awaiting ACK
   */
  assigned: {
    type: 'runner_status' as const,
    payload: {
      id: testRunner.id,
      status: 'assigned' as RunnerState,
      current_step_id: 'step-001',
    },
  },

  /**
   * Runner is executing a step
   */
  busy: {
    type: 'runner_status' as const,
    payload: {
      id: testRunner.id,
      status: 'busy' as RunnerState,
      current_step_id: 'step-001',
    },
  },

  /**
   * Runner disconnected gracefully (WebSocket closed)
   */
  disconnected: {
    type: 'runner_status' as const,
    payload: {
      id: testRunner.id,
      status: 'disconnected' as RunnerState,
    },
  },

  /**
   * Runner presumed crashed (heartbeat timeout)
   */
  dead: {
    type: 'runner_status' as const,
    payload: {
      id: testRunner.id,
      status: 'dead' as RunnerState,
    },
  },

  /**
   * Runner reconnecting after disconnect/dead
   */
  connecting: {
    type: 'runner_status' as const,
    payload: {
      id: testRunner.id,
      status: 'connecting' as RunnerState,
    },
  },
};

/**
 * Complete lifecycle: connect -> work -> disconnect
 */
export const runnerLifecycleSequence = [
  runnerStatusMessages.connecting,
  runnerStatusMessages.idle,
  runnerStatusMessages.assigned,
  runnerStatusMessages.busy,
  runnerStatusMessages.idle,  // After completing step
  runnerStatusMessages.disconnected,
];

/**
 * Error scenario: runner dies mid-execution
 */
export const runnerDeathSequence = [
  runnerStatusMessages.idle,
  runnerStatusMessages.assigned,
  runnerStatusMessages.busy,
  runnerStatusMessages.dead,  // Heartbeat timeout
];

/**
 * Recovery scenario: dead runner reconnects
 */
export const runnerRecoverySequence = [
  runnerStatusMessages.dead,
  runnerStatusMessages.connecting,
  runnerStatusMessages.idle,
];
