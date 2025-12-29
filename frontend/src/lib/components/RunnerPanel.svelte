<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { poolStatus, runnersStore } from '../stores/runners';
  import { runners as runnersApi } from '../api/client';
  import type { DockerCommand, Runner } from '../api/types';

  let showRunners = true;
  let showDockerModal = false;
  let showLogsModal = false;
  let dockerCommand: DockerCommand | null = null;
  let copyFeedback = '';
  let selectedRunner: Runner | null = null;
  let logs: string[] = [];
  let logsLoading = false;
  let logPollInterval: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    poolStatus.startPolling(2000);
    runnersStore.startPolling(2000);
  });

  onDestroy(() => {
    poolStatus.stopPolling();
    runnersStore.stopPolling();
    stopLogPolling();
  });

  async function openDockerModal() {
    showDockerModal = true;
    try {
      dockerCommand = await runnersApi.dockerCommand(false);
    } catch (e) {
      console.error('Failed to get docker command:', e);
    }
  }

  async function copyCommand(withSecrets: boolean) {
    if (!dockerCommand) return;
    try {
      if (withSecrets) {
        dockerCommand = await runnersApi.dockerCommand(true);
      }
      const cmd = withSecrets ? dockerCommand.command_with_secrets : dockerCommand.command;
      await navigator.clipboard.writeText(cmd);
      copyFeedback = withSecrets ? 'Copied with secrets!' : 'Copied!';
      setTimeout(() => copyFeedback = '', 2000);
    } catch (e) {
      console.error('Failed to copy:', e);
    }
  }

  async function openLogsModal(runner: Runner) {
    selectedRunner = runner;
    showLogsModal = true;
    logs = [];
    await loadLogs();
    startLogPolling();
  }

  function closeLogsModal() {
    showLogsModal = false;
    selectedRunner = null;
    stopLogPolling();
  }

  async function loadLogs() {
    if (!selectedRunner) return;
    logsLoading = true;
    try {
      const result = await runnersApi.logs(selectedRunner.id);
      logs = result.logs;
    } catch (e) {
      console.error('Failed to load logs:', e);
    } finally {
      logsLoading = false;
    }
  }

  function startLogPolling() {
    if (logPollInterval) return;
    logPollInterval = setInterval(loadLogs, 2000);
  }

  function stopLogPolling() {
    if (logPollInterval) {
      clearInterval(logPollInterval);
      logPollInterval = null;
    }
  }

  function getStatusColor(status: string): string {
    switch (status) {
      case 'idle': return 'var(--success-color, #a6e3a1)';
      case 'busy': return 'var(--warning-color, #f9e2af)';
      case 'offline': return 'var(--error-color, #f38ba8)';
      default: return 'var(--text-muted, #6c7086)';
    }
  }
</script>

<div class="runner-panel">
  <div class="panel-header">
    <h2>Runners</h2>
    <button class="btn-icon" on:click={openDockerModal} title="Get Docker command">
      🐳
    </button>
  </div>

  {#if $poolStatus}
    <div class="pool-stats">
      <div class="stat">
        <span class="stat-value">{$poolStatus.total_runners}</span>
        <span class="stat-label">Total</span>
      </div>
      <div class="stat">
        <span class="stat-value idle">{$poolStatus.idle_runners}</span>
        <span class="stat-label">Idle</span>
      </div>
      <div class="stat">
        <span class="stat-value busy">{$poolStatus.busy_runners}</span>
        <span class="stat-label">Busy</span>
      </div>
      <div class="stat">
        <span class="stat-value queued">{$poolStatus.queued_jobs}</span>
        <span class="stat-label">Queued</span>
      </div>
    </div>
  {:else}
    <div class="loading">Loading...</div>
  {/if}

  <button
    class="btn-toggle"
    on:click={() => showRunners = !showRunners}
  >
    {showRunners ? '▼' : '▶'} Runners ({$runnersStore.length})
  </button>

  {#if showRunners}
    <div class="runner-list">
      {#each $runnersStore as runner (runner.id)}
        <button
          class="runner-item"
          on:click={() => openLogsModal(runner)}
        >
          <span
            class="status-dot"
            style="background: {getStatusColor(runner.status)}"
          ></span>
          <span class="runner-name">{runner.name}</span>
          <span class="runner-status">{runner.status}</span>
          {#if runner.current_job_id}
            <span class="job-badge">working</span>
          {/if}
          {#if runner.log_count > 0}
            <span class="log-count">{runner.log_count} lines</span>
          {/if}
        </button>
      {:else}
        <div class="no-runners">
          <p>No runners connected</p>
          <p class="hint">Click 🐳 to get the Docker command</p>
        </div>
      {/each}
    </div>
  {/if}
</div>

<!-- Docker Command Modal -->
{#if showDockerModal}
  <div
    class="modal-backdrop"
    on:click={() => showDockerModal = false}
    on:keydown={(e) => e.key === 'Escape' && (showDockerModal = false)}
    role="dialog"
    aria-modal="true"
    tabindex="-1"
  >
    <div class="modal" on:click|stopPropagation role="document">
      <div class="modal-header">
        <h3>Start a Runner</h3>
        <button class="btn-close" on:click={() => showDockerModal = false}>✕</button>
      </div>

      <div class="modal-body">
        <p class="modal-description">
          Run this command to start a runner that will connect to the backend and wait for jobs.
        </p>

        {#if dockerCommand}
          <div class="command-box">
            <code>{dockerCommand.command}</code>
          </div>

          <div class="modal-actions">
            <button class="btn-secondary" on:click={() => copyCommand(false)}>
              📋 Copy
            </button>
            <button class="btn-primary" on:click={() => copyCommand(true)}>
              🔐 Copy with Secrets
            </button>
          </div>

          {#if copyFeedback}
            <div class="copy-feedback">{copyFeedback}</div>
          {/if}

          <div class="env-vars">
            <h4>Environment Variables</h4>
            <ul>
              <li><code>BACKEND_URL</code> - Backend API URL (default: http://host.docker.internal:8000)</li>
              <li><code>ANTHROPIC_API_KEY</code> - Your Anthropic API key (required for jobs)</li>
              <li><code>GITHUB_TOKEN</code> - GitHub token for creating PRs (optional)</li>
              <li><code>RUNNER_NAME</code> - Custom name for the runner (optional)</li>
            </ul>
          </div>
        {:else}
          <div class="loading">Loading command...</div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<!-- Logs Modal -->
{#if showLogsModal && selectedRunner}
  <div
    class="modal-backdrop"
    on:click={closeLogsModal}
    on:keydown={(e) => e.key === 'Escape' && closeLogsModal()}
    role="dialog"
    aria-modal="true"
    tabindex="-1"
  >
    <div class="modal modal-logs" on:click|stopPropagation role="document">
      <div class="modal-header">
        <div class="logs-header-info">
          <h3>{selectedRunner.name}</h3>
          <span
            class="status-badge"
            style="background: {getStatusColor(selectedRunner.status)}20; color: {getStatusColor(selectedRunner.status)}"
          >
            {selectedRunner.status}
          </span>
        </div>
        <button class="btn-close" on:click={closeLogsModal}>✕</button>
      </div>

      <div class="logs-container">
        {#if logs.length > 0}
          <pre class="logs-content">{logs.join('\n')}</pre>
        {:else if logsLoading}
          <div class="logs-empty">Loading logs...</div>
        {:else}
          <div class="logs-empty">No logs yet</div>
        {/if}
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
    font-size: 1rem;
  }

  .btn-icon:hover {
    background: var(--hover-color, #313244);
  }

  .pool-stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
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
  .stat-value.queued { color: var(--primary-color, #89b4fa); }

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
    max-height: 300px;
    overflow-y: auto;
  }

  .runner-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    width: 100%;
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    color: inherit;
  }

  .runner-item:hover {
    background: var(--hover-color, #313244);
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .runner-name {
    font-family: monospace;
    color: var(--text-color, #cdd6f4);
  }

  .runner-status {
    color: var(--text-muted, #6c7086);
    text-transform: capitalize;
  }

  .job-badge {
    background: var(--warning-color, #f9e2af);
    color: var(--primary-text, #1e1e2e);
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-size: 0.7rem;
    font-weight: 500;
  }

  .log-count {
    margin-left: auto;
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
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

  .loading {
    padding: 1rem;
    text-align: center;
    color: var(--text-muted, #6c7086);
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

  .modal-logs {
    max-width: 800px;
    height: 80vh;
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

  .logs-header-info {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .status-badge {
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 500;
    text-transform: capitalize;
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

  .logs-container {
    flex: 1;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .logs-content {
    flex: 1;
    margin: 0;
    padding: 1rem;
    background: var(--surface-alt, #181825);
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .logs-empty {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted, #6c7086);
  }

  .modal-description {
    margin: 0 0 1rem;
    color: var(--text-muted, #6c7086);
    font-size: 0.9rem;
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
    font-size: 0.85rem;
    color: var(--text-color, #cdd6f4);
    white-space: pre-wrap;
    word-break: break-all;
  }

  .modal-actions {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

  .btn-primary, .btn-secondary {
    flex: 1;
    padding: 0.6rem 1rem;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    border: none;
  }

  .btn-primary {
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
  }

  .btn-secondary {
    background: var(--surface-alt, #313244);
    color: var(--text-color, #cdd6f4);
  }

  .btn-primary:hover, .btn-secondary:hover {
    opacity: 0.9;
  }

  .copy-feedback {
    text-align: center;
    color: var(--success-color, #a6e3a1);
    font-size: 0.85rem;
    margin-bottom: 1rem;
  }

  .env-vars {
    border-top: 1px solid var(--border-color, #45475a);
    padding-top: 1rem;
  }

  .env-vars h4 {
    margin: 0 0 0.5rem;
    font-size: 0.9rem;
    color: var(--text-color, #cdd6f4);
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
