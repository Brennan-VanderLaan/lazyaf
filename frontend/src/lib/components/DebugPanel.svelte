<script lang="ts">
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import type { DebugSessionInfo, DebugSessionStatus } from '../api/types';
  import { debug as debugApi } from '../api/client';
  import { debugStore } from '../stores/debug';

  export let sessionId: string;
  export let token: string;

  const dispatch = createEventDispatcher<{
    close: void;
    resumed: void;
    aborted: void;
  }>();

  let session: DebugSessionInfo | null = null;
  let isLoading = true;
  let error: string | null = null;
  let actionLoading: 'resume' | 'abort' | 'extend' | null = null;
  let copied = false;
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let timeRemaining: string = '';

  $: isAtBreakpoint = session?.status === 'waiting_at_bp' || session?.status === 'connected';
  $: isTerminal = session?.status === 'timeout' || session?.status === 'ended';

  // Subscribe to WebSocket debug events
  $: debugSessions = $debugStore;
  $: debugSessionFromWs = debugSessions.get(sessionId);

  // React to WebSocket updates
  $: if (debugSessionFromWs && session) {
    if (debugSessionFromWs.status !== session.status) {
      // Status changed via WebSocket - fetch full session data
      loadSession();
    }
    if (debugSessionFromWs.status === 'resumed') {
      dispatch('resumed');
    }
  }

  onMount(() => {
    loadSession();
    // Poll for updates every 5 seconds (increased since WebSocket provides real-time updates)
    pollInterval = setInterval(loadSession, 5000);
    // Update time remaining every second
    const timeInterval = setInterval(updateTimeRemaining, 1000);
    return () => {
      if (pollInterval) clearInterval(pollInterval);
      clearInterval(timeInterval);
    };
  });

  onDestroy(() => {
    if (pollInterval) clearInterval(pollInterval);
  });

  async function loadSession() {
    try {
      session = await debugApi.getSession(sessionId);
      error = null;
      updateTimeRemaining();

      // Stop polling if terminal state
      if (isTerminal && pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load session';
    } finally {
      isLoading = false;
    }
  }

  function updateTimeRemaining() {
    if (!session?.expires_at) {
      timeRemaining = '';
      return;
    }

    const expires = new Date(session.expires_at).getTime();
    const now = Date.now();
    const diff = expires - now;

    if (diff <= 0) {
      timeRemaining = 'Expired';
      return;
    }

    const minutes = Math.floor(diff / 60000);
    const seconds = Math.floor((diff % 60000) / 1000);
    timeRemaining = `${minutes}:${seconds.toString().padStart(2, '0')}`;
  }

  async function handleResume() {
    actionLoading = 'resume';
    try {
      await debugApi.resume(sessionId);
      dispatch('resumed');
      await loadSession();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to resume';
    } finally {
      actionLoading = null;
    }
  }

  async function handleAbort() {
    actionLoading = 'abort';
    try {
      await debugApi.abort(sessionId);
      dispatch('aborted');
      await loadSession();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to abort';
    } finally {
      actionLoading = null;
    }
  }

  async function handleExtend() {
    actionLoading = 'extend';
    try {
      await debugApi.extend(sessionId, 30);
      await loadSession();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to extend timeout';
    } finally {
      actionLoading = null;
    }
  }

  function copyCommand() {
    if (session?.join_command) {
      navigator.clipboard.writeText(session.join_command);
      copied = true;
      setTimeout(() => copied = false, 2000);
    }
  }

  function getStatusColor(status: DebugSessionStatus): string {
    switch (status) {
      case 'pending': return 'var(--text-secondary, #999)';
      case 'waiting_at_bp': return 'var(--warning-color, #ffa500)';
      case 'connected': return 'var(--success-color, #4caf50)';
      case 'timeout': return 'var(--error-color, #ff6b6b)';
      case 'ended': return 'var(--text-secondary, #666)';
      default: return 'var(--text-secondary, #999)';
    }
  }

  function getStatusText(status: DebugSessionStatus): string {
    switch (status) {
      case 'pending': return 'Starting...';
      case 'waiting_at_bp': return 'Waiting at Breakpoint';
      case 'connected': return 'Connected';
      case 'timeout': return 'Timed Out';
      case 'ended': return 'Ended';
      default: return status;
    }
  }
</script>

<div class="debug-panel">
  <div class="header">
    <h3>Debug Session</h3>
    <button class="close-btn" on:click={() => dispatch('close')}>x</button>
  </div>

  {#if isLoading && !session}
    <div class="loading">Loading session...</div>
  {:else if error}
    <div class="error">{error}</div>
  {:else if session}
    <div class="content">
      <!-- Status Section -->
      <div class="section status-section">
        <div class="status-indicator" style="--status-color: {getStatusColor(session.status)}">
          <span class="status-dot"></span>
          <span class="status-text">{getStatusText(session.status)}</span>
        </div>
        {#if timeRemaining && !isTerminal}
          <div class="timeout-display">
            <span class="timeout-label">Timeout:</span>
            <span class="timeout-value" class:warning={timeRemaining.startsWith('0:') || timeRemaining.startsWith('1:')}>{timeRemaining}</span>
          </div>
        {/if}
      </div>

      <!-- Current Step Section -->
      {#if session.current_step}
        <div class="section">
          <h4>Breakpoint</h4>
          <div class="step-info">
            <span class="step-name">{session.current_step.name}</span>
            <span class="step-meta">Step {session.current_step.index + 1} ({session.current_step.type})</span>
          </div>
        </div>
      {/if}

      <!-- Commit Section -->
      <div class="section">
        <h4>Commit</h4>
        <div class="commit-info">
          <code class="commit-sha">{session.commit.sha.substring(0, 8)}</code>
          <span class="commit-message">{session.commit.message}</span>
        </div>
      </div>

      <!-- Runtime Section -->
      <div class="section">
        <h4>Runtime</h4>
        <div class="runtime-info">
          <div class="runtime-row">
            <span class="runtime-label">Host:</span>
            <span class="runtime-value">{session.runtime.host}</span>
          </div>
          <div class="runtime-row">
            <span class="runtime-label">Orchestrator:</span>
            <span class="runtime-value">{session.runtime.orchestrator}</span>
          </div>
          <div class="runtime-row">
            <span class="runtime-label">Image:</span>
            <span class="runtime-value">{session.runtime.image}</span>
          </div>
        </div>
      </div>

      <!-- Join Command Section -->
      {#if isAtBreakpoint}
        <div class="section">
          <h4>Connect via CLI</h4>
          <div class="join-command">
            <code>{session.join_command}</code>
            <button class="copy-btn" on:click={copyCommand}>
              {copied ? 'Copied!' : 'Copy'}
            </button>
          </div>
          <p class="help-text">
            Use <code>--sidecar</code> to inspect files or <code>--shell</code> to exec into the running container.
          </p>
        </div>
      {/if}

      <!-- Logs Section -->
      {#if session.logs}
        <div class="section logs-section">
          <h4>Logs</h4>
          <pre class="logs">{session.logs}</pre>
        </div>
      {/if}

      <!-- Actions -->
      {#if isAtBreakpoint}
        <div class="actions">
          <button
            class="action-btn extend-btn"
            on:click={handleExtend}
            disabled={actionLoading !== null}
          >
            {actionLoading === 'extend' ? 'Extending...' : '+30 min'}
          </button>
          <button
            class="action-btn abort-btn"
            on:click={handleAbort}
            disabled={actionLoading !== null}
          >
            {actionLoading === 'abort' ? 'Aborting...' : 'Abort'}
          </button>
          <button
            class="action-btn resume-btn"
            on:click={handleResume}
            disabled={actionLoading !== null}
          >
            {actionLoading === 'resume' ? 'Resuming...' : 'Resume'}
          </button>
        </div>
      {/if}

      {#if isTerminal}
        <div class="terminal-notice">
          {#if session.status === 'timeout'}
            <p>This debug session has timed out. Start a new debug re-run to continue debugging.</p>
          {:else}
            <p>This debug session has ended.</p>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .debug-panel {
    background: var(--bg-color, #1e1e1e);
    border: 1px solid var(--border-color, #3e3e3e);
    border-radius: 8px;
    overflow: hidden;
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-color, #3e3e3e);
    background: var(--header-bg, #252525);
  }

  .header h3 {
    margin: 0;
    font-size: 14px;
  }

  .close-btn {
    background: none;
    border: none;
    color: var(--text-color, #ccc);
    font-size: 18px;
    cursor: pointer;
    padding: 2px 6px;
  }

  .close-btn:hover {
    color: var(--text-primary, #fff);
  }

  .content {
    padding: 16px;
  }

  .loading, .error {
    padding: 24px;
    text-align: center;
  }

  .error {
    color: var(--error-color, #ff6b6b);
  }

  .section {
    margin-bottom: 16px;
  }

  .section h4 {
    margin: 0 0 8px 0;
    font-size: 12px;
    color: var(--text-secondary, #999);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .status-section {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px;
    background: var(--section-bg, #252525);
    border-radius: 4px;
  }

  .status-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--status-color);
  }

  .status-text {
    font-weight: 500;
  }

  .timeout-display {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .timeout-label {
    color: var(--text-secondary, #999);
    font-size: 12px;
  }

  .timeout-value {
    font-family: monospace;
    font-size: 14px;
  }

  .timeout-value.warning {
    color: var(--warning-color, #ffa500);
  }

  .step-info {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .step-name {
    font-weight: 500;
  }

  .step-meta {
    color: var(--text-secondary, #999);
    font-size: 12px;
  }

  .commit-info {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .commit-sha {
    background: var(--code-bg, #2a2a2a);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 12px;
  }

  .commit-message {
    color: var(--text-secondary, #ccc);
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .runtime-info {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .runtime-row {
    display: flex;
    gap: 8px;
    font-size: 13px;
  }

  .runtime-label {
    color: var(--text-secondary, #999);
    min-width: 90px;
  }

  .runtime-value {
    color: var(--text-color, #ccc);
    font-family: monospace;
  }

  .join-command {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--code-bg, #1a1a1a);
    padding: 12px;
    border-radius: 4px;
    overflow-x: auto;
  }

  .join-command code {
    flex: 1;
    font-size: 12px;
    white-space: nowrap;
  }

  .copy-btn {
    background: var(--accent-color, #007acc);
    border: none;
    color: white;
    padding: 4px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    white-space: nowrap;
  }

  .copy-btn:hover {
    background: var(--accent-hover, #0096e0);
  }

  .help-text {
    margin: 8px 0 0 0;
    font-size: 12px;
    color: var(--text-secondary, #999);
  }

  .help-text code {
    background: var(--code-bg, #2a2a2a);
    padding: 1px 4px;
    border-radius: 2px;
  }

  .logs-section {
    max-height: 200px;
  }

  .logs {
    background: var(--code-bg, #1a1a1a);
    padding: 12px;
    border-radius: 4px;
    font-size: 12px;
    overflow: auto;
    max-height: 150px;
    margin: 0;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 16px;
    border-top: 1px solid var(--border-color, #3e3e3e);
  }

  .action-btn {
    padding: 8px 16px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
    border: none;
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .extend-btn {
    background: var(--section-bg, #2a2a2a);
    color: var(--text-color, #ccc);
    border: 1px solid var(--border-color, #3e3e3e);
  }

  .extend-btn:hover:not(:disabled) {
    background: var(--hover-bg, #333);
  }

  .abort-btn {
    background: var(--error-bg, #3a2020);
    color: var(--error-color, #ff6b6b);
  }

  .abort-btn:hover:not(:disabled) {
    background: #4a2020;
  }

  .resume-btn {
    background: var(--accent-color, #007acc);
    color: white;
  }

  .resume-btn:hover:not(:disabled) {
    background: var(--accent-hover, #0096e0);
  }

  .terminal-notice {
    padding: 16px;
    text-align: center;
    color: var(--text-secondary, #999);
    border-top: 1px solid var(--border-color, #3e3e3e);
  }

  .terminal-notice p {
    margin: 0;
  }
</style>
