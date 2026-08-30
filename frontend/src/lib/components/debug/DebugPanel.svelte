<script lang="ts">
  /**
   * Live state of the debug session gating one pipeline run (Phase 12.7).
   *
   * Mounted inside the run viewer. It is the surface that makes a pause
   * HONEST: at a breakpoint the run's own `StepRun` still reads `running`
   * (the executor committed and broadcast it before the gate fired), so
   * without this panel a wedged pipeline is indistinguishable from a slow
   * one. Three things it therefore always shows: WHICH step is held, HOW LONG
   * the hold has left, and - once the session is over - WHY it ended.
   *
   * It renders no terminal. Terminal I/O is the CLI's path (the join command
   * below is the handoff); a browser xterm would be a second implementation
   * of the frame protocol for no new capability.
   */
  import { onMount, onDestroy } from 'svelte';
  import type { DebugSessionInfo, PipelineRun } from '../../api/types';
  import {
    debugSessionsStore,
    debugSessionForRun,
    debugStateLabel,
    isPausedDebugState,
    isTerminalDebugState,
    remainingMs,
    formatCountdown,
    debugNow,
  } from '../../stores/debug';

  export let run: PipelineRun;

  /** Sub-minute is the point at which "plenty of time" becomes a decision. */
  const URGENT_MS = 60_000;

  let session: DebugSessionInfo | null = null;
  let boundRunId: string | null = null;
  let unsubscribe: (() => void) | null = null;
  let busy: string | null = null;
  let actionError: string | null = null;
  let copyState: 'idle' | 'copied' | 'failed' = 'idle';
  let clampNotice: string | null = null;

  // Re-bind when the viewer is reused for a different run. Written as a call
  // rather than a reactive BLOCK on purpose: a block that both reads and
  // writes `unsubscribe` declares itself as its own dependency, and this one
  // has a live subscription on the other side of that loop.
  $: bindToRun(run.id);

  function bindToRun(runId: string) {
    if (runId === boundRunId) return;
    boundRunId = runId;
    unsubscribe?.();
    unsubscribe = debugSessionForRun(runId).subscribe((value) => (session = value));
  }

  onMount(() => {
    // Snapshot half of snapshot-then-delta: a session parked at a breakpoint
    // broadcasts nothing, so a reload during a pause has no delta to learn
    // from and only this fetch can populate the panel.
    debugSessionsStore.load();
  });

  onDestroy(() => unsubscribe?.());

  $: paused = session !== null && isPausedDebugState(session.status);
  $: ended = session !== null && isTerminalDebugState(session.status);
  $: msLeft = session ? remainingMs(session.expires_at, $debugNow) : null;
  $: urgent = msLeft !== null && msLeft <= URGENT_MS;

  async function act(label: string, fn: () => Promise<unknown>) {
    busy = label;
    actionError = null;
    try {
      await fn();
    } catch (e) {
      actionError = e instanceof Error ? e.message : `${label} failed`;
    } finally {
      busy = null;
    }
  }

  const resume = () =>
    act('resume', () => debugSessionsStore.resume(session!.id, false));

  const resumeAll = () =>
    act('resume-all', () => debugSessionsStore.resume(session!.id, true));

  const abort = () => act('abort', () => debugSessionsStore.abort(session!.id));

  const extend = () =>
    act('extend', async () => {
      const response = await debugSessionsStore.extend(session!.id, 30);
      // The backend clamps to max_timeout_seconds. Saying "+30m" when it
      // granted less would be a lie the countdown then contradicts.
      clampNotice = response.clamped
        ? 'Extension trimmed — the session is at its maximum lifetime.'
        : null;
    });

  /**
   * Clipboard access is permission-gated and absent over plain HTTP on some
   * browsers. A failure says so and leaves the command selectable rather than
   * pretending the copy happened.
   */
  async function copyJoinCommand() {
    if (!session) return;
    try {
      await navigator.clipboard.writeText(session.join_command);
      copyState = 'copied';
      setTimeout(() => (copyState = 'idle'), 2000);
    } catch {
      copyState = 'failed';
    }
  }
</script>

{#if session}
  <section
    class="debug-panel"
    data-testid="debug-panel"
    data-status={session.status}
    class:paused
    class:ended
  >
    <header class="panel-header">
      <span class="badge" data-testid="debug-state-label">{debugStateLabel(session.status)}</span>
      {#if msLeft !== null && !ended}
        <span class="countdown" data-testid="debug-countdown" class:urgent>
          {formatCountdown(msLeft)} left
        </span>
      {/if}
    </header>

    {#if session.current_step}
      <p class="current-step" data-testid="debug-current-step" data-step-key={session.current_step.key}>
        {paused ? 'Paused before' : 'At'}
        <strong>{session.current_step.name}</strong>
        {#if session.current_step.type}
          <span class="step-type">{session.current_step.type}</span>
        {/if}
      </p>
    {/if}

    <dl class="context">
      <div>
        <dt>Commit</dt>
        <dd data-testid="debug-commit">
          <!--
            A run started against a branch head has no pinned sha yet, so the
            branch IS the answer to "what is this debugging" - printing a dash
            there would be the panel refusing to say what it knows.
          -->
          <code>{session.commit.sha ? session.commit.sha.substring(0, 8) : session.commit.branch || '-'}</code>
          {#if session.commit.message}
            <span class="commit-message">{session.commit.message}</span>
          {/if}
        </dd>
      </div>
      <div>
        <dt>Runtime</dt>
        <dd data-testid="debug-runtime">
          {session.runtime.host} · {session.runtime.orchestrator} · <code>{session.runtime.image}</code>
        </dd>
      </div>
    </dl>

    <div class="breakpoints">
      <span data-testid="debug-breakpoints-hit">Hit: {session.breakpoints_hit.length}</span>
      <span data-testid="debug-breakpoints-pending">
        Pending: {session.breakpoints_pending.length}
      </span>
      <span class="all-keys" title={session.breakpoints.join(', ')}>
        of {session.breakpoints.length} breakpoint{session.breakpoints.length === 1 ? '' : 's'}
      </span>
    </div>

    {#if paused}
      {#if session.attach_available}
        <div class="join">
          <code class="join-command" data-testid="debug-join-command">{session.join_command}</code>
          <button type="button" class="copy-btn" data-testid="debug-copy-join" on:click={copyJoinCommand}>
            {copyState === 'copied' ? 'Copied' : 'Copy'}
          </button>
        </div>
        {#if copyState === 'failed'}
          <p class="copy-failed" data-testid="debug-copy-failed">
            Clipboard unavailable — select the command above and copy it manually.
          </p>
        {/if}
      {:else}
        <!--
          R1: a reduced capability is stated, never silently degraded. The
          backend refuses the terminal upgrade with this same sentence, so the
          UI and the WS close reason cannot drift into disagreeing.
        -->
        <p class="attach-unavailable" data-testid="debug-attach-unavailable">
          {session.attach_unavailable_reason ??
            'Terminal attach is unavailable for this step and the backend gave no reason — treat that as a bug.'}
        </p>
      {/if}
    {/if}

    {#if clampNotice}
      <p class="end-reason" data-testid="debug-extend-clamped">{clampNotice}</p>
    {/if}

    {#if ended && session.end_reason}
      <p class="end-reason" data-testid="debug-end-reason">Ended: {session.end_reason}</p>
    {/if}

    {#if actionError}
      <p class="error" data-testid="debug-error">{actionError}</p>
    {/if}

    {#if !ended}
      <div class="actions">
        <button
          type="button"
          class="btn-primary"
          data-testid="debug-resume-btn"
          on:click={resume}
          disabled={!paused || busy !== null}
        >
          {busy === 'resume' ? 'Resuming…' : 'Resume'}
        </button>
        <button
          type="button"
          class="btn-secondary"
          data-testid="debug-resume-all-btn"
          on:click={resumeAll}
          disabled={!paused || busy !== null}
          title="Drop the remaining breakpoints and run to completion"
        >
          Run to completion
        </button>
        <button
          type="button"
          class="btn-secondary"
          data-testid="debug-extend-btn"
          on:click={extend}
          disabled={busy !== null}
        >
          +30m
        </button>
        <button
          type="button"
          class="btn-danger"
          data-testid="debug-abort-btn"
          on:click={abort}
          disabled={busy !== null}
          title="End the session and cancel the run"
        >
          Abort
        </button>
      </div>
    {/if}
  </section>
{/if}

<style>
  .debug-panel {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.9rem 1rem;
    margin-bottom: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    background: var(--bg-color);
  }

  .debug-panel.paused {
    border-color: var(--warning-color);
  }

  .debug-panel.ended {
    opacity: 0.85;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .badge {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    border: 1px solid var(--border-color);
    color: var(--text-muted);
  }

  .debug-panel.paused .badge {
    color: var(--warning-color);
    border-color: var(--warning-color);
  }

  .countdown {
    font-family: monospace;
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .countdown.urgent {
    color: var(--error-color);
    font-weight: 600;
  }

  .current-step {
    margin: 0;
    font-size: 0.9rem;
  }

  .step-type {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-left: 0.35rem;
  }

  .context {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 1.5rem;
    font-size: 0.8rem;
  }

  .context div {
    display: flex;
    gap: 0.4rem;
    align-items: baseline;
  }

  .context dt {
    color: var(--text-muted);
  }

  .context dd {
    margin: 0;
    display: flex;
    gap: 0.4rem;
    align-items: baseline;
  }

  .commit-message {
    color: var(--text-muted);
    max-width: 22rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .breakpoints {
    display: flex;
    gap: 1rem;
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .join {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .join-command {
    flex: 1;
    padding: 0.4rem 0.6rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 0.8rem;
    overflow-x: auto;
    white-space: nowrap;
  }

  .copy-btn {
    padding: 0.35rem 0.7rem;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    background: transparent;
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.8rem;
  }

  .copy-failed,
  .attach-unavailable,
  .end-reason,
  .error {
    margin: 0;
    font-size: 0.8rem;
    line-height: 1.4;
  }

  .attach-unavailable {
    padding: 0.5rem 0.65rem;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    color: var(--text-muted);
  }

  .end-reason {
    color: var(--text-muted);
  }

  .error {
    color: var(--error-color);
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .actions button {
    padding: 0.4rem 0.8rem;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .btn-primary {
    background: var(--primary-color, #4a9eff);
    border-color: var(--primary-color, #4a9eff);
    color: #fff;
  }

  .btn-secondary {
    background: transparent;
    color: var(--text-color);
  }

  .btn-danger {
    background: transparent;
    color: var(--error-color);
    border-color: var(--error-color);
  }

  .actions button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
