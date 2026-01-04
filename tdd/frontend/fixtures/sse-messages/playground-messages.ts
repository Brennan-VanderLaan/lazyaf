/**
 * SSE (Server-Sent Events) fixtures for Agent Playground tests.
 *
 * IMPORTANT: The Playground uses SSE, NOT WebSocket!
 * SSE is unidirectional (server → client) and uses a different format.
 *
 * SSE Format:
 *   event: <event-type>\n
 *   data: <json-payload>\n
 *   \n
 *
 * These fixtures return properly formatted SSE strings for use with
 * page.route() in Playwright tests.
 */

/**
 * Playground session status values
 */
export type PlaygroundStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

/**
 * Sample playground test configuration
 */
export const testPlaygroundConfig = {
  session_id: 'playground-session-001',
  repo_id: 'repo-test-001',
  branch: 'main',
  runner_type: 'claude-code',
  model: 'claude-sonnet-4-5-20250929',
  task: 'Fix the login bug in auth.ts',
};

// =============================================================================
// SSE Event Helpers
// =============================================================================

/**
 * Create a properly formatted SSE event string
 */
export function createSSEEvent(eventType: string, data: object): string {
  return `event: ${eventType}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * Create a log event
 */
export function createLogEvent(line: string): string {
  return createSSEEvent('log', { line });
}

/**
 * Create a logs_batch event
 */
export function createLogsBatchEvent(lines: string[]): string {
  return createSSEEvent('logs_batch', { lines });
}

/**
 * Create a status event
 */
export function createStatusEvent(status: PlaygroundStatus): string {
  return createSSEEvent('status', { status });
}

/**
 * Create a complete event
 */
export function createCompleteEvent(status: 'completed' | 'failed', diff?: string, error?: string): string {
  return createSSEEvent('complete', {
    status,
    ...(diff && { diff }),
    ...(error && { error }),
  });
}

/**
 * Create an error event
 */
export function createErrorEvent(error: string): string {
  return createSSEEvent('error', { error });
}

/**
 * Create a ping event (keep-alive)
 */
export function createPingEvent(): string {
  return createSSEEvent('ping', {});
}

// =============================================================================
// Pre-built Status Events
// =============================================================================

export const playgroundStatusEvents = {
  queued: createStatusEvent('queued'),
  running: createStatusEvent('running'),
  completed: createStatusEvent('completed'),
  failed: createStatusEvent('failed'),
  cancelled: createStatusEvent('cancelled'),
};

// =============================================================================
// Pre-built Log Events
// =============================================================================

export const playgroundLogEvents = {
  startup: createLogsBatchEvent([
    '[2024-01-15 10:00:00] Starting playground session...',
    '[2024-01-15 10:00:01] Session ID: playground-session-001',
    '[2024-01-15 10:00:02] Cloning repository...',
  ]),

  cloning: createLogsBatchEvent([
    '[2024-01-15 10:00:03] Checking out branch: main',
    '[2024-01-15 10:00:04] Repository ready',
  ]),

  agentStart: createLogsBatchEvent([
    '[2024-01-15 10:00:05] Initializing agent...',
    '[2024-01-15 10:00:06] Model: claude-sonnet-4-5-20250929',
    '[2024-01-15 10:00:07] Task: Fix the login bug in auth.ts',
  ]),

  agentOutput: createLogsBatchEvent([
    '[2024-01-15 10:00:10] Agent: Analyzing codebase...',
    '[2024-01-15 10:00:15] Agent: Found auth.ts with login function',
    '[2024-01-15 10:00:20] Agent: Identified issue in password validation',
    '[2024-01-15 10:00:25] Agent: Applying fix...',
  ]),

  progress: createLogsBatchEvent([
    '[2024-01-15 10:00:30] Modified: src/auth.ts',
    '[2024-01-15 10:00:31] Running tests...',
    '[2024-01-15 10:00:40] Tests passed: 5/5',
  ]),

  completion: createLogsBatchEvent([
    '[2024-01-15 10:00:45] Agent completed successfully',
    '[2024-01-15 10:00:46] Files changed: 1',
    '[2024-01-15 10:00:47] Session complete',
  ]),

  error: createLogsBatchEvent([
    '[2024-01-15 10:00:30] ERROR: Agent execution failed',
    '[2024-01-15 10:00:31] Error: Unable to parse file',
    '[2024-01-15 10:00:32] Session terminated with error',
  ]),
};

// =============================================================================
// Sample Diff Output
// =============================================================================

export const sampleDiff = `diff --git a/src/auth.ts b/src/auth.ts
index 1234567..abcdefg 100644
--- a/src/auth.ts
+++ b/src/auth.ts
@@ -15,7 +15,7 @@ export function validatePassword(password: string): boolean {
-  if (password.length < 6) {
+  if (password.length < 8) {
     return false;
   }
+  // Added: require at least one number
+  if (!/\\d/.test(password)) {
+    return false;
+  }
   return true;
 }`;

// =============================================================================
// Complete SSE Sequences (as strings for route.fulfill)
// =============================================================================

/**
 * Successful playground run - full SSE sequence
 */
export const successfulPlaygroundSSE = [
  playgroundStatusEvents.queued,
  playgroundStatusEvents.running,
  playgroundLogEvents.startup,
  playgroundLogEvents.cloning,
  playgroundLogEvents.agentStart,
  playgroundLogEvents.agentOutput,
  playgroundLogEvents.progress,
  playgroundLogEvents.completion,
  createCompleteEvent('completed', sampleDiff),
].join('');

/**
 * Failed playground run - SSE sequence
 */
export const failedPlaygroundSSE = [
  playgroundStatusEvents.queued,
  playgroundStatusEvents.running,
  playgroundLogEvents.startup,
  playgroundLogEvents.cloning,
  playgroundLogEvents.agentStart,
  playgroundLogEvents.error,
  createCompleteEvent('failed', undefined, 'Agent execution failed: Unable to parse file'),
].join('');

/**
 * Cancelled playground run - SSE sequence
 */
export const cancelledPlaygroundSSE = [
  playgroundStatusEvents.queued,
  playgroundStatusEvents.running,
  playgroundLogEvents.startup,
  playgroundLogEvents.cloning,
  playgroundStatusEvents.cancelled,
].join('');

/**
 * Minimal successful run (for quick tests)
 */
export const minimalSuccessSSE = [
  playgroundStatusEvents.running,
  createLogEvent('Processing...'),
  createLogEvent('Done!'),
  createCompleteEvent('completed', sampleDiff),
].join('');

/**
 * Long-running session with many logs (for auto-scroll tests)
 */
export function generateManyLogsSSE(lineCount: number): string {
  const events = [playgroundStatusEvents.running];

  for (let i = 0; i < lineCount; i++) {
    events.push(createLogEvent(`[${new Date().toISOString()}] Log line ${i + 1} of ${lineCount}`));
  }

  events.push(createCompleteEvent('completed', sampleDiff));
  return events.join('');
}

// =============================================================================
// API Response Mocks (for non-SSE endpoints)
// =============================================================================

/**
 * Response from POST /api/repos/{id}/playground/test
 */
export const startTestResponse = {
  session_id: testPlaygroundConfig.session_id,
  status: 'queued' as PlaygroundStatus,
};

/**
 * Response from GET /api/playground/{session}/status
 */
export const statusResponses = {
  queued: { status: 'queued', elapsed_seconds: 0 },
  running: { status: 'running', elapsed_seconds: 15 },
  completed: { status: 'completed', elapsed_seconds: 45, files_changed: 1 },
  failed: { status: 'failed', elapsed_seconds: 30, error: 'Agent execution failed' },
};

/**
 * Response from GET /api/playground/{session}/result
 */
export const resultResponse = {
  status: 'completed',
  diff: sampleDiff,
  files_changed: ['src/auth.ts'],
  elapsed_seconds: 45,
};
