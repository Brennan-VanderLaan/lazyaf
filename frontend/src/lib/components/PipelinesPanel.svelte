<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { pipelinesStore, activeRunsStore, hasActiveRuns } from '../stores/pipelines';
  import { selectedRepoId } from '../stores/repos';
  import type { Pipeline, PipelineRun, RunStatus, RepoPipeline } from '../api/types';
  import { lazyafFiles } from '../api/client';
  import PipelineEditor from './PipelineEditor.svelte';
  import PipelineRunViewer from './PipelineRunViewer.svelte';

  let expanded = false;
  let showEditor = false;
  let editingPipeline: Pipeline | null = null;
  let viewingRun: PipelineRun | null = null;
  let repoPipelines: RepoPipeline[] = [];
  let repoPipelinesError: string | null = null;

  // Load recent runs on mount (regardless of selected repo)
  onMount(() => {
    activeRunsStore.loadRecent();
  });

  // Load pipelines when repo changes
  $: if ($selectedRepoId) {
    pipelinesStore.load($selectedRepoId);
    loadRepoPipelines($selectedRepoId);
  }

  async function loadRepoPipelines(repoId: string) {
    console.log('[PipelinesPanel] Loading repo pipelines for:', repoId);
    repoPipelinesError = null;
    try {
      repoPipelines = await lazyafFiles.listPipelines(repoId);
      console.log('[PipelinesPanel] Loaded repo pipelines:', repoPipelines);
    } catch (e) {
      console.error('[PipelinesPanel] Failed to load repo pipelines:', e);
      repoPipelinesError = e instanceof Error ? e.message : 'Failed to load repo pipelines';
      repoPipelines = [];
    }
  }

  // Refresh runs periodically when there are active runs
  let refreshInterval: ReturnType<typeof setInterval> | null = null;

  $: if ($hasActiveRuns) {
    if (!refreshInterval) {
      refreshInterval = setInterval(() => {
        activeRunsStore.loadRecent();
      }, 3000);
    }
  } else if (refreshInterval) {
    clearInterval(refreshInterval);
    refreshInterval = null;
  }

  onDestroy(() => {
    if (refreshInterval) {
      clearInterval(refreshInterval);
    }
  });

  function handleCreate() {
    editingPipeline = null;
    showEditor = true;
  }

  function handleEdit(pipeline: Pipeline) {
    editingPipeline = pipeline;
    showEditor = true;
  }

  async function handleRun(pipeline: Pipeline) {
    try {
      const run = await pipelinesStore.run(pipeline.id);
      viewingRun = run;
    } catch (e) {
      // Error is set in store
    }
  }

  function handleViewRun(run: PipelineRun) {
    viewingRun = run;
  }

  function getStatusColor(status: RunStatus): string {
    switch (status) {
      case 'pending': return 'var(--text-muted)';
      case 'running': return 'var(--warning-color)';
      case 'passed': return 'var(--success-color)';
      case 'failed': return 'var(--error-color)';
      case 'cancelled': return 'var(--text-muted)';
      default: return 'var(--text-muted)';
    }
  }

  function getStatusIcon(status: RunStatus): string {
    switch (status) {
      case 'pending': return '◯';
      case 'running': return '⟳';
      case 'passed': return '✓';
      case 'failed': return '✗';
      case 'cancelled': return '⊘';
      default: return '?';
    }
  }

  // Get recent runs for display
  $: recentRuns = Array.from($activeRunsStore.values())
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 5);
</script>

<div class="panel">
  <button class="panel-header" on:click={() => expanded = !expanded}>
    <span class="panel-title">
      <span class="panel-icon">{$hasActiveRuns ? '⚙️' : '📋'}</span>
      Pipelines
    </span>
    <span class="expand-icon">{expanded ? '▼' : '▶'}</span>
  </button>

  {#if expanded}
    <div class="panel-content">
      {#if !$selectedRepoId}
        <p class="no-repo">Select a repo to manage pipelines</p>
      {:else}
        <!-- Repo-defined pipelines -->
        {#if repoPipelines.length > 0}
          <div class="section">
            <div class="section-header">
              <span>From Repository</span>
            </div>
            <div class="pipeline-list">
              {#each repoPipelines as pipeline}
                <div class="pipeline-item repo-pipeline">
                  <div class="pipeline-info">
                    <span class="pipeline-name">
                      {pipeline.name}
                      <span class="repo-badge">repo</span>
                    </span>
                    <span class="pipeline-steps">{pipeline.steps.length} steps</span>
                  </div>
                  <div class="pipeline-actions">
                    <span class="pipeline-hint" title="Repo pipelines are read-only. Edit in .lazyaf/pipelines/">📁</span>
                  </div>
                </div>
              {/each}
            </div>
          </div>
        {/if}

        {#if repoPipelinesError}
          <p class="error-text">{repoPipelinesError}</p>
        {/if}

        <!-- Platform pipelines -->
        <div class="section">
          <div class="section-header">
            <span>Platform Pipelines</span>
            <button class="btn-small" on:click={handleCreate}>+ New</button>
          </div>

          {#if $pipelinesStore.length === 0}
            <p class="empty">No platform pipelines yet</p>
          {:else}
            <div class="pipeline-list">
              {#each $pipelinesStore as pipeline}
                <div class="pipeline-item">
                  <div class="pipeline-info">
                    <span class="pipeline-name">{pipeline.name}</span>
                    <span class="pipeline-steps">{pipeline.steps.length} steps</span>
                  </div>
                  <div class="pipeline-actions">
                    <button class="btn-icon" title="Edit" on:click={() => handleEdit(pipeline)}>✏️</button>
                    <button class="btn-icon" title="Run" on:click={() => handleRun(pipeline)}>▶</button>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>

        {#if recentRuns.length > 0}
          <div class="section">
            <div class="section-header">
              <span>Recent Runs</span>
            </div>
            <div class="runs-list">
              {#each recentRuns as run}
                <button class="run-item" on:click={() => handleViewRun(run)}>
                  <span class="run-status" style="color: {getStatusColor(run.status as RunStatus)}">
                    {getStatusIcon(run.status as RunStatus)}
                  </span>
                  <span class="run-info">
                    <span class="run-progress">{run.steps_completed}/{run.steps_total}</span>
                    <span class="run-status-text">{run.status}</span>
                  </span>
                </button>
              {/each}
            </div>
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</div>

{#if showEditor}
  <PipelineEditor
    repoId={$selectedRepoId || ''}
    pipeline={editingPipeline}
    on:close={() => showEditor = false}
    on:created={() => showEditor = false}
    on:updated={() => showEditor = false}
    on:deleted={() => showEditor = false}
  />
{/if}

{#if viewingRun}
  <PipelineRunViewer
    run={viewingRun}
    on:close={() => viewingRun = null}
  />
{/if}

<style>
  .panel {
    border-bottom: 1px solid var(--border-color);
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: 0.75rem 1rem;
    background: none;
    border: none;
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.9rem;
    text-align: left;
  }

  .panel-header:hover {
    background: var(--hover-color);
  }

  .panel-title {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 600;
  }

  .panel-icon {
    font-size: 1rem;
  }

  .expand-icon {
    font-size: 0.7rem;
    color: var(--text-muted);
  }

  .panel-content {
    padding: 0.5rem 1rem 1rem;
  }

  .no-repo, .empty {
    color: var(--text-muted);
    font-size: 0.85rem;
    margin: 0;
  }

  .section {
    margin-bottom: 1rem;
  }

  .section:last-child {
    margin-bottom: 0;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
    font-size: 0.8rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .btn-small {
    padding: 0.2rem 0.5rem;
    font-size: 0.75rem;
    background: var(--primary-color);
    color: var(--primary-text);
    border: none;
    border-radius: 4px;
    cursor: pointer;
  }

  .btn-small:hover {
    opacity: 0.9;
  }

  .pipeline-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .pipeline-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem;
    background: var(--surface-alt);
    border-radius: 6px;
  }

  .pipeline-info {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .pipeline-name {
    font-size: 0.9rem;
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .pipeline-steps {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .repo-badge {
    font-size: 0.65rem;
    padding: 0.1rem 0.3rem;
    background: var(--primary-color);
    color: var(--primary-text);
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }

  .repo-pipeline {
    border-left: 2px solid var(--primary-color);
  }

  .pipeline-hint {
    opacity: 0.6;
    cursor: help;
  }

  .error-text {
    color: var(--error-color);
    font-size: 0.8rem;
    margin: 0.5rem 0;
  }

  .pipeline-actions {
    display: flex;
    gap: 0.25rem;
  }

  .btn-icon {
    padding: 0.3rem;
    background: none;
    border: none;
    cursor: pointer;
    border-radius: 4px;
    font-size: 0.9rem;
  }

  .btn-icon:hover {
    background: var(--hover-color);
  }

  .runs-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .run-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.5rem;
    background: var(--surface-alt);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    width: 100%;
    text-align: left;
    color: var(--text-color);
  }

  .run-item:hover {
    background: var(--hover-color);
  }

  .run-status {
    font-size: 0.9rem;
    font-weight: bold;
  }

  .run-info {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    font-size: 0.8rem;
  }

  .run-progress {
    font-family: monospace;
  }

  .run-status-text {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
</style>
