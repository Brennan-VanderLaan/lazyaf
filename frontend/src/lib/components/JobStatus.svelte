<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import type { Job, JobStatus as JobStatusType } from '../api/types';
  import { jobs as jobsApi } from '../api/client';
  import { jobsStore, getJobForCard } from '../stores/jobs';

  export let cardId: string;
  export let jobId: string | null;

  let job: Job | null = null;
  let logs: string = '';
  let showLogs = false;
  let loadingLogs = false;
  let logsError: string | null = null;
  let logsInterval: ReturnType<typeof setInterval> | null = null;

  // Subscribe to the job store for this card
  $: jobFromStore = jobId ? getJobForCard(cardId) : null;
  $: if ($jobFromStore) {
    job = $jobFromStore;
  }

  // Load job on mount if we have a jobId
  onMount(async () => {
    if (jobId) {
      try {
        job = await jobsStore.load(jobId);
      } catch (e) {
        // Job might not exist yet or be loading
      }
    }
  });

  onDestroy(() => {
    if (logsInterval) {
      clearInterval(logsInterval);
    }
  });

  async function loadLogs() {
    if (!jobId) return;
    loadingLogs = true;
    logsError = null;
    try {
      const response = await jobsApi.logs(jobId);
      logs = response.logs;
    } catch (e) {
      logsError = e instanceof Error ? e.message : 'Failed to load logs';
    } finally {
      loadingLogs = false;
    }
  }

  async function toggleLogs() {
    showLogs = !showLogs;
    if (showLogs) {
      await loadLogs();
      // Poll for logs while viewing and job is running
      if (job?.status === 'running' || job?.status === 'queued') {
        logsInterval = setInterval(loadLogs, 3000);
      }
    } else {
      if (logsInterval) {
        clearInterval(logsInterval);
        logsInterval = null;
      }
    }
  }

  // Stop polling when job completes
  $: if (job && (job.status === 'completed' || job.status === 'failed') && logsInterval) {
    clearInterval(logsInterval);
    logsInterval = null;
    // Load final logs
    if (showLogs) {
      loadLogs();
    }
  }

  const statusLabels: Record<JobStatusType, string> = {
    queued: 'Queued',
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
  };

  const statusIcons: Record<JobStatusType, string> = {
    queued: '⏳',
    running: '⚙️',
    completed: '✓',
    failed: '✗',
  };

  function formatDuration(startedAt: string | null, completedAt: string | null): string {
    if (!startedAt) return '';
    const start = new Date(startedAt);
    const end = completedAt ? new Date(completedAt) : new Date();
    const seconds = Math.floor((end.getTime() - start.getTime()) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;
    return `${minutes}m ${remainingSeconds}s`;
  }
</script>

{#if job || jobId}
  <div class="job-status" data-status={job?.status ?? 'queued'}>
    <div class="job-header">
      <div class="job-info">
        <span class="job-icon">{job ? statusIcons[job.status] : '⏳'}</span>
        <span class="job-label">
          {job ? statusLabels[job.status] : 'Loading...'}
        </span>
        {#if job?.status === 'running' || job?.status === 'queued'}
          <span class="spinner"></span>
        {/if}
      </div>
      {#if job?.started_at}
        <span class="job-duration">
          {formatDuration(job.started_at, job.completed_at)}
        </span>
      {/if}
    </div>

    {#if job?.error}
      <div class="job-error">
        {job.error}
      </div>
    {/if}

    <button class="btn-logs" on:click={toggleLogs} type="button">
      {showLogs ? 'Hide Logs' : 'View Logs'}
    </button>

    {#if showLogs}
      <div class="job-logs">
        {#if job?.status === 'queued'}
          <div class="logs-empty">Waiting for runner to pick up job...</div>
        {:else if loadingLogs && !logs}
          <div class="logs-loading">Loading logs...</div>
        {:else if logsError}
          <div class="logs-error">{logsError}</div>
        {:else if logs}
          <pre>{logs}</pre>
          {#if job?.status === 'running'}
            <div class="logs-streaming">Streaming... (updates every 3s)</div>
          {/if}
        {:else}
          <div class="logs-empty">No logs available yet</div>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .job-status {
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1.25rem;
    border-left: 3px solid var(--border-color, #45475a);
  }

  .job-status[data-status="queued"] {
    border-left-color: #89b4fa;
  }

  .job-status[data-status="running"] {
    border-left-color: #f9e2af;
  }

  .job-status[data-status="completed"] {
    border-left-color: #a6e3a1;
  }

  .job-status[data-status="failed"] {
    border-left-color: #f38ba8;
  }

  .job-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .job-info {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .job-icon {
    font-size: 1.1rem;
  }

  .job-label {
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
  }

  .job-duration {
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
    font-family: monospace;
  }

  .spinner {
    width: 14px;
    height: 14px;
    border: 2px solid var(--text-muted, #6c7086);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .job-error {
    background: #f38ba822;
    border: 1px solid #f38ba844;
    border-radius: 4px;
    padding: 0.5rem 0.75rem;
    margin: 0.5rem 0;
    font-size: 0.85rem;
    color: #f38ba8;
  }

  .btn-logs {
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    color: var(--text-muted, #6c7086);
    padding: 0.4rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    margin-top: 0.5rem;
  }

  .btn-logs:hover {
    color: var(--text-color, #cdd6f4);
    border-color: var(--text-muted, #6c7086);
  }

  .job-logs {
    margin-top: 0.75rem;
    background: var(--surface-color, #1e1e2e);
    border-radius: 6px;
    max-height: 300px;
    overflow: auto;
  }

  .job-logs pre {
    margin: 0;
    padding: 0.75rem;
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.8rem;
    line-height: 1.5;
    color: var(--text-color, #cdd6f4);
    white-space: pre-wrap;
    word-break: break-all;
  }

  .logs-loading,
  .logs-empty,
  .logs-error {
    padding: 1rem;
    text-align: center;
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
  }

  .logs-error {
    color: #f38ba8;
  }

  .logs-streaming {
    padding: 0.5rem 0.75rem;
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
    border-top: 1px solid var(--border-color, #45475a);
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .logs-streaming::before {
    content: '';
    width: 8px;
    height: 8px;
    background: #a6e3a1;
    border-radius: 50%;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }
</style>
