<script lang="ts">
  /**
   * RunnerPanel - Phase 12.6.
   *
   * Two whole features left with the polling stack rather than being ported:
   *
   *  - the DOCKER-COMMAND modal handed an operator a `docker run` line for a
   *    polling runner image. Those images are gone; a 12.6 runner is a
   *    `lazyaf-runner` agent enrolled over `/ws/runner` with a shared secret,
   *    and pasting a half-configured command out of the UI is not how a host
   *    joins a fleet.
   *  - the LOG modal polled `GET /api/runners/{id}/logs`, an endpoint that
   *    served the polling pool's in-memory ring buffer. A runner's output is
   *    now its steps' output, read on the pipeline run where it belongs.
   *
   * What replaces them is what an operator actually needs to answer "can this
   * fleet take my pinned step": state, labels, the step being executed, and
   * how long the socket has been up.
   *
   * Data flow is SNAPSHOT-THEN-DELTA: one `GET /api/runners` on mount, then
   * `runner_status` frames merged by the store (see stores/runners.ts). There
   * is no interval in this component.
   */
  import { onMount } from 'svelte';
  import { runnersStore, connectedRunners, busyRunners, idleRunners } from '../stores/runners';
  import type { Runner, RunnerState } from '../api/types';

  let showRunners = true;
  /** Ticks once a second purely so "connected 4m ago" stays honest. */
  let now = Date.now();

  // The store's list is the default export value; loading/loaded/error are
  // sibling stores on it, so they have to be bound to locals for `$` to
  // auto-subscribe.
  const runnersLoading = runnersStore.loading;
  const runnersLoaded = runnersStore.loaded;
  const runnersError = runnersStore.error;

  $: groupedRunners = $runnersStore.reduce((acc, runner) => {
    const type = runner.runner_type || 'unknown';
    (acc[type] ||= []).push(runner);
    return acc;
  }, {} as Record<string, Runner[]>);

  $: runnerTypes = Object.keys(groupedRunners).sort();

  onMount(() => {
    // The snapshot. Without it a reload renders an empty panel over a live
    // fleet until some runner happens to change state.
    runnersStore.load();
    const tick = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(tick);
  });

  function getStatusColor(status: RunnerState): string {
    switch (status) {
      case 'idle': return 'var(--success-color, #a6e3a1)';
      case 'busy':
      case 'assigned': return 'var(--warning-color, #f9e2af)';
      case 'connecting': return 'var(--primary-color, #89b4fa)';
      case 'dead': return 'var(--error-color, #f38ba8)';
      case 'disconnected': return 'var(--text-muted, #6c7086)';
      default: return 'var(--text-muted, #6c7086)';
    }
  }

  /** `{arch: 'amd64', has: ['docker','gpio']}` -> ['arch=amd64','has=docker','has=gpio'] */
  function labelChips(labels: Record<string, unknown> | null | undefined): string[] {
    if (!labels) return [];
    const chips: string[] = [];
    for (const [key, value] of Object.entries(labels)) {
      if (Array.isArray(value)) {
        for (const item of value) chips.push(`${key}=${item}`);
      } else if (value !== null && value !== undefined && value !== '') {
        chips.push(`${key}=${value}`);
      }
    }
    return chips;
  }

  function connectionAge(runner: Runner, atMs: number): string {
    if (!runner.connected_at) return '';
    const started = Date.parse(runner.connected_at);
    if (Number.isNaN(started)) return '';
    const seconds = Math.max(0, Math.floor((atMs - started) / 1000));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  }
</script>

<div class="runner-panel" data-testid="runner-panel">
  <div class="panel-header">
    <h2>Runners</h2>
  </div>

  <div class="pool-stats" data-testid="pool-stats">
    <div class="stat">
      <span class="stat-value" data-testid="stat-connected">{$connectedRunners.length}</span>
      <span class="stat-label">Connected</span>
    </div>
    <div class="stat">
      <span class="stat-value idle" data-testid="stat-idle">{$idleRunners.length}</span>
      <span class="stat-label">Idle</span>
    </div>
    <div class="stat">
      <span class="stat-value busy" data-testid="stat-busy">{$busyRunners.length}</span>
      <span class="stat-label">Busy</span>
    </div>
  </div>

  <button class="btn-toggle" on:click={() => (showRunners = !showRunners)}>
    {showRunners ? '▼' : '▶'} Runners ({$runnersStore.length})
  </button>

  {#if showRunners}
    <div class="runner-list">
      {#if $runnersStore.length === 0}
        <div class="no-runners" data-testid="no-runners">
          {#if $runnersLoading && !$runnersLoaded}
            <p>Loading runners…</p>
          {:else}
            <p>No runners connected</p>
            <p class="hint">
              Start one with <code>lazyaf-runner --backend-url …</code>; it enrolls
              over the runner WebSocket.
            </p>
          {/if}
        </div>
      {:else}
        {#each runnerTypes as runnerType (runnerType)}
          <div class="runner-group">
            <div class="runner-group-header">
              <span class="runner-type-label">{runnerType}</span>
              <span class="runner-count">{groupedRunners[runnerType].length}</span>
            </div>
            {#each groupedRunners[runnerType] as runner (runner.id)}
              <div
                class="runner-item"
                data-testid="runner-item"
                data-runner-id={runner.id}
                data-status={runner.status}
                data-connection={runner.connection}
              >
                <span class="status-dot" style="background: {getStatusColor(runner.status)}"></span>
                <div class="runner-info">
                  <div class="runner-main">
                    <span class="runner-name">{runner.name || runner.id}</span>
                    <span class="runner-status" data-testid="runner-status">{runner.status}</span>
                  </div>

                  {#if runner.current_step_execution_id}
                    <div class="runner-job" data-testid="runner-current-step">
                      <span class="job-icon">⚡</span>
                      <span class="job-title">step {runner.current_step_execution_id.slice(0, 8)}</span>
                    </div>
                  {/if}

                  {#if labelChips(runner.labels).length}
                    <div class="runner-labels" data-testid="runner-labels">
                      {#each labelChips(runner.labels) as chip (chip)}
                        <span class="label-chip">{chip}</span>
                      {/each}
                    </div>
                  {/if}

                  <div class="runner-meta">
                    {#if runner.connection === 'websocket'}
                      <span class="meta-item" data-testid="runner-connection" title="live WebSocket held by this backend process">
                        ws {connectionAge(runner, now)}
                      </span>
                    {:else}
                      <span class="meta-item stale" data-testid="runner-connection" title="no live socket in this backend process - the row is stale">
                        no socket
                      </span>
                    {/if}
                    {#if runner.agent_version}
                      <span class="meta-item">agent {runner.agent_version}</span>
                    {/if}
                    {#if runner.protocol_version !== null}
                      <span class="meta-item">v{runner.protocol_version}</span>
                    {/if}
                  </div>
                </div>
              </div>
            {/each}
          </div>
        {/each}
      {/if}
    </div>
  {/if}

  {#if $runnersError}
    <div class="panel-error" data-testid="runner-error">{$runnersError}</div>
  {/if}
</div>

<style>
  .runner-panel {
    background: var(--surface-color, #1e1e2e);
    border-radius: 8px;
    padding: 1rem;
    margin-top: 1rem;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .panel-header h2 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--text-color, #cdd6f4);
  }

  .pool-stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }

  .stat {
    text-align: center;
    padding: 0.5rem;
    background: var(--surface-alt, #181825);
    border-radius: 6px;
  }

  .stat-value {
    display: block;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-color, #cdd6f4);
  }

  .stat-value.idle { color: var(--success-color, #a6e3a1); }
  .stat-value.busy { color: var(--warning-color, #f9e2af); }

  .stat-label {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    text-transform: uppercase;
  }

  .btn-toggle {
    width: 100%;
    padding: 0.5rem;
    background: none;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    color: var(--text-muted, #6c7086);
    font-size: 0.8rem;
    cursor: pointer;
    text-align: left;
  }

  .btn-toggle:hover {
    background: var(--hover-color, #313244);
  }

  .runner-list {
    margin-top: 0.5rem;
    max-height: 400px;
    overflow-y: auto;
  }

  .runner-group {
    margin-bottom: 0.75rem;
  }

  .runner-group:last-child {
    margin-bottom: 0;
  }

  .runner-group-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.4rem 0.5rem;
    margin-bottom: 0.25rem;
    background: var(--surface-alt, #181825);
    border-radius: 4px;
  }

  .runner-type-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--primary-color, #89b4fa);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .runner-count {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    background: var(--surface-color, #1e1e2e);
    padding: 0.1rem 0.4rem;
    border-radius: 10px;
  }

  .runner-item {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    padding: 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    width: 100%;
    text-align: left;
    color: inherit;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 0.3rem;
    align-self: flex-start;
  }

  .runner-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .runner-main {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.5rem;
  }

  .runner-name {
    color: var(--text-color, #cdd6f4);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .runner-status {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    text-transform: uppercase;
    letter-spacing: 0.4px;
    flex-shrink: 0;
  }

  .runner-job {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.72rem;
    color: var(--warning-color, #f9e2af);
  }

  .job-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .runner-labels {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
  }

  .label-chip {
    font-size: 0.65rem;
    font-family: var(--font-mono, monospace);
    color: var(--primary-color, #89b4fa);
    background: var(--surface-alt, #181825);
    border-radius: 3px;
    padding: 0.05rem 0.3rem;
  }

  .runner-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    font-size: 0.65rem;
    color: var(--text-muted, #6c7086);
  }

  .meta-item.stale {
    color: var(--error-color, #f38ba8);
  }

  .no-runners {
    text-align: center;
    padding: 1rem 0.5rem;
    color: var(--text-muted, #6c7086);
    font-size: 0.8rem;
  }

  .no-runners p {
    margin: 0.2rem 0;
  }

  .no-runners .hint {
    font-size: 0.72rem;
  }

  .no-runners code {
    font-family: var(--font-mono, monospace);
    background: var(--surface-alt, #181825);
    border-radius: 3px;
    padding: 0 0.2rem;
  }

  .panel-error {
    margin-top: 0.5rem;
    font-size: 0.72rem;
    color: var(--error-color, #f38ba8);
  }
</style>
