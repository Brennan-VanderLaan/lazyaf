<script lang="ts">
  /**
   * Runner Panel - Phase 12.6 WebSocket-based Architecture
   *
   * Displays connected runners and their status.
   * Runner data is pushed via WebSocket - no polling required.
   */
  import { poolStatus, runnersStore, connectedRunners } from '../stores/runners';
  import type { Runner, RunnerStatus } from '../api/types';

  let showRunners = true;
  let showHelpModal = false;

  // Group runners by type
  $: groupedRunners = $runnersStore.reduce((acc, runner) => {
    const type = runner.runner_type || 'unknown';
    if (!acc[type]) {
      acc[type] = [];
    }
    acc[type].push(runner);
    return acc;
  }, {} as Record<string, Runner[]>);

  $: runnerTypes = Object.keys(groupedRunners).sort();

  function getStatusColor(status: RunnerStatus): string {
    switch (status) {
      case 'idle': return 'var(--success-color, #a6e3a1)';
      case 'busy':
      case 'assigned': return 'var(--warning-color, #f9e2af)';
      case 'connecting': return 'var(--primary-color, #89b4fa)';
      case 'disconnected': return 'var(--text-muted, #6c7086)';
      case 'dead': return 'var(--error-color, #f38ba8)';
      default: return 'var(--text-muted, #6c7086)';
    }
  }

  function getStatusLabel(status: RunnerStatus): string {
    switch (status) {
      case 'idle': return 'Ready';
      case 'busy': return 'Executing';
      case 'assigned': return 'Starting...';
      case 'connecting': return 'Connecting';
      case 'disconnected': return 'Offline';
      case 'dead': return 'Dead';
      default: return status;
    }
  }
</script>

<div class="runner-panel">
  <div class="panel-header">
    <h2>Runners</h2>
    <button class="btn-icon" on:click={() => showHelpModal = true} title="How to start a runner">
      ?
    </button>
  </div>

  <div class="pool-stats">
    <div class="stat">
      <span class="stat-value">{$connectedRunners.length}</span>
      <span class="stat-label">Connected</span>
    </div>
    <div class="stat">
      <span class="stat-value idle">{$poolStatus.idle_runners}</span>
      <span class="stat-label">Ready</span>
    </div>
    <div class="stat">
      <span class="stat-value busy">{$poolStatus.busy_runners}</span>
      <span class="stat-label">Busy</span>
    </div>
  </div>

  <button
    class="btn-toggle"
    on:click={() => showRunners = !showRunners}
  >
    {showRunners ? '▼' : '▶'} Runners ({$runnersStore.length})
  </button>

  {#if showRunners}
    <div class="runner-list">
      {#if $runnersStore.length === 0}
        <div class="no-runners">
          <p>No runners connected</p>
          <p class="hint">Click ? to see how to start a runner</p>
        </div>
      {:else}
        {#each runnerTypes as runnerType (runnerType)}
          <div class="runner-group">
            <div class="runner-group-header">
              <span class="runner-type-label">{runnerType}</span>
              <span class="runner-count">{groupedRunners[runnerType].length}</span>
            </div>
            {#each groupedRunners[runnerType] as runner (runner.id)}
              <div class="runner-item">
                <span
                  class="status-dot"
                  style="background: {getStatusColor(runner.status)}"
                ></span>
                <div class="runner-info">
                  <div class="runner-main">
                    <span class="runner-name">{runner.name || runner.id.slice(0, 8)}</span>
                    <span
                      class="runner-status"
                      style="color: {getStatusColor(runner.status)}"
                    >
                      {getStatusLabel(runner.status)}
                    </span>
                  </div>
                  {#if runner.current_step_execution_id}
                    <div class="runner-job">
                      <span class="job-icon">⚡</span>
                      <span class="job-title">Executing step</span>
                    </div>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/each}
      {/if}
    </div>
  {/if}
</div>

<!-- Help Modal -->
{#if showHelpModal}
  <div
    class="modal-backdrop"
    on:click={() => showHelpModal = false}
    on:keydown={(e) => e.key === 'Escape' && (showHelpModal = false)}
    role="dialog"
    aria-modal="true"
    tabindex="-1"
  >
    <div class="modal" on:click|stopPropagation role="document">
      <div class="modal-header">
        <h3>Starting a Runner</h3>
        <button class="btn-close" on:click={() => showHelpModal = false}>✕</button>
      </div>

      <div class="modal-body">
        <p class="modal-description">
          Runners connect to the backend via WebSocket and execute pipeline steps.
        </p>

        <h4>Using Docker (Recommended)</h4>
        <div class="command-box">
          <code>docker run --rm -it \
  -e LAZYAF_BACKEND_URL=http://host.docker.internal:8000 \
  -e ANTHROPIC_API_KEY=your-key \
  -v /var/run/docker.sock:/var/run/docker.sock \
  lazyaf-runner-agent</code>
        </div>

        <h4>Using Python Directly</h4>
        <div class="command-box">
          <code>export LAZYAF_BACKEND_URL="http://localhost:8000"
export LAZYAF_RUNNER_ID="my-runner"
export ANTHROPIC_API_KEY="your-key"
python -m lazyaf_runner</code>
        </div>

        <div class="env-vars">
          <h4>Environment Variables</h4>
          <ul>
            <li><code>LAZYAF_BACKEND_URL</code> - Backend WebSocket URL (required)</li>
            <li><code>LAZYAF_RUNNER_ID</code> - Optional runner ID</li>
            <li><code>LAZYAF_LABELS</code> - Labels for job routing (e.g., "arch=arm64,has=gpio")</li>
            <li><code>ANTHROPIC_API_KEY</code> - For Claude Code runners</li>
            <li><code>GEMINI_API_KEY</code> - For Gemini runners</li>
          </ul>
        </div>
      </div>
    </div>
  </div>
{/if}

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

  .btn-icon {
    background: none;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    cursor: pointer;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.9rem;
    font-weight: 600;
  }

  .btn-icon:hover {
    background: var(--hover-color, #313244);
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
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
  }

  .runner-item:hover {
    background: var(--hover-color, #313244);
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 0.2rem;
    align-self: flex-start;
  }

  .runner-info {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
  }

  .runner-main {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .runner-name {
    font-family: monospace;
    color: var(--text-color, #cdd6f4);
    font-size: 0.8rem;
  }

  .runner-status {
    text-transform: capitalize;
    font-size: 0.75rem;
  }

  .runner-job {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.4rem;
    background: rgba(137, 180, 250, 0.1);
    border-radius: 3px;
    border-left: 2px solid var(--primary-color, #89b4fa);
  }

  .job-icon {
    font-size: 0.7rem;
  }

  .job-title {
    font-size: 0.75rem;
    color: var(--text-color, #cdd6f4);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .no-runners {
    padding: 1rem;
    text-align: center;
    color: var(--text-muted, #6c7086);
  }

  .no-runners p {
    margin: 0 0 0.25rem;
  }

  .no-runners .hint {
    font-size: 0.8rem;
    opacity: 0.7;
  }

  /* Modal styles */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }

  .modal {
    background: var(--surface-color, #1e1e2e);
    border-radius: 12px;
    width: 100%;
    max-width: 600px;
    max-height: 90vh;
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    display: flex;
    flex-direction: column;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.25rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    flex-shrink: 0;
  }

  .modal-header h3 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--text-color, #cdd6f4);
  }

  .btn-close {
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    font-size: 1.25rem;
    cursor: pointer;
  }

  .modal-body {
    padding: 1.25rem;
    overflow-y: auto;
  }

  .modal-description {
    margin: 0 0 1rem;
    color: var(--text-muted, #6c7086);
    font-size: 0.9rem;
  }

  .modal-body h4 {
    margin: 1rem 0 0.5rem;
    font-size: 0.9rem;
    color: var(--text-color, #cdd6f4);
  }

  .modal-body h4:first-of-type {
    margin-top: 0;
  }

  .command-box {
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 1rem;
    overflow-x: auto;
  }

  .command-box code {
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
    white-space: pre-wrap;
    word-break: break-all;
  }

  .env-vars {
    border-top: 1px solid var(--border-color, #45475a);
    padding-top: 1rem;
    margin-top: 1rem;
  }

  .env-vars ul {
    margin: 0;
    padding-left: 1.25rem;
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
  }

  .env-vars li {
    margin-bottom: 0.25rem;
  }

  .env-vars code {
    background: var(--surface-alt, #181825);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    font-size: 0.8rem;
  }
</style>
