<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { pipelinesStore, activeRunsStore, hasActiveRuns } from '../stores/pipelines';
  import { selectedRepoId, selectedRepo } from '../stores/repos';
  import type { Pipeline, PipelineRun, RunStatus, RepoPipeline } from '../api/types';
  import { lazyafFiles } from '../api/client';
  import PipelineEditor from '../components/PipelineEditor.svelte';
  import PipelineRunViewer from '../components/PipelineRunViewer.svelte';

  type TabType = 'pipelines' | 'runs';
  let activeTab: TabType = 'pipelines';
  let showEditor = false;
  let editingPipeline: Pipeline | null = null;
  let viewingRun: PipelineRun | null = null;
  let repoPipelines: RepoPipeline[] = [];
  let repoPipelinesLoading = false;

  // Load recent runs on mount
  onMount(() => {
    activeRunsStore.loadRecent();
  });

  // Load pipelines when repo changes
  $: if ($selectedRepoId) {
    pipelinesStore.load($selectedRepoId);
    loadRepoPipelines($selectedRepoId);
  }

  async function loadRepoPipelines(repoId: string) {
    repoPipelinesLoading = true;
    try {
      repoPipelines = await lazyafFiles.listPipelines(repoId);
    } catch (e) {
      console.error('Failed to load repo pipelines:', e);
      repoPipelines = [];
    } finally {
      repoPipelinesLoading = false;
    }
  }

  async function handleRunRepoPipeline(pipeline: RepoPipeline) {
    if (!$selectedRepoId || !pipeline.filename) return;
    try {
      // Extract pipeline name from filename (remove .yaml/.yml extension)
      const pipelineName = pipeline.filename.replace(/\.(yaml|yml)$/, '');
      const result = await lazyafFiles.runPipeline($selectedRepoId, pipelineName);
      // Refresh runs and switch to runs tab
      activeRunsStore.loadRecent();
      activeTab = 'runs';
    } catch (e) {
      console.error('Failed to run repo pipeline:', e);
      alert(`Failed to run pipeline: ${e instanceof Error ? e.message : 'Unknown error'}`);
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

  function formatDate(dateStr: string): string {
    const date = new Date(dateStr);
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function formatDuration(start: string | null, end: string | null): string {
    if (!start) return '-';
    const startTime = new Date(start).getTime();
    const endTime = end ? new Date(end).getTime() : Date.now();
    const seconds = Math.floor((endTime - startTime) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${seconds % 60}s`;
  }

  // Get all runs sorted by date
  $: allRuns = Array.from($activeRunsStore.values())
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
</script>

<div class="pipelines-page">
  <header class="page-header">
    <div class="header-left">
      <h1>Pipelines</h1>
      {#if $selectedRepo}
        <span class="repo-badge">{$selectedRepo.name}</span>
      {/if}
    </div>
    {#if $selectedRepoId}
      <button class="btn-primary" on:click={handleCreate}>
        + New Pipeline
      </button>
    {/if}
  </header>

  {#if !$selectedRepoId}
    <div class="empty-state">
      <span class="empty-icon">📋</span>
      <p>Select a repository to manage pipelines</p>
    </div>
  {:else}
    <nav class="tabs">
      <button
        class="tab"
        class:active={activeTab === 'pipelines'}
        on:click={() => activeTab = 'pipelines'}
      >
        Pipelines ({$pipelinesStore.length})
      </button>
      <button
        class="tab"
        class:active={activeTab === 'runs'}
        on:click={() => activeTab = 'runs'}
      >
        Runs ({allRuns.length})
        {#if $hasActiveRuns}
          <span class="active-indicator"></span>
        {/if}
      </button>
    </nav>

    <div class="tab-content">
      {#if activeTab === 'pipelines'}
        <!-- Repo-defined pipelines -->
        {#if repoPipelines.length > 0}
          <div class="section-header">
            <h2>From Repository</h2>
            <span class="section-hint">.lazyaf/pipelines/</span>
          </div>
          <div class="pipelines-grid">
            {#each repoPipelines as pipeline}
              <div class="pipeline-card repo-card">
                <div class="card-header">
                  <h3>
                    {pipeline.name}
                    <span class="repo-source-badge">repo</span>
                  </h3>
                  <div class="card-actions">
                    <span class="read-only-hint" title="Edit in .lazyaf/pipelines/{pipeline.filename}">📁</span>
                    <button class="btn-run" title="Run" on:click={() => handleRunRepoPipeline(pipeline)}>
                      Run
                    </button>
                  </div>
                </div>
                {#if pipeline.description}
                  <p class="card-description">{pipeline.description}</p>
                {/if}
                <div class="card-meta">
                  <span class="step-count">{pipeline.steps.length} steps</span>
                  <div class="step-types">
                    {#each [...new Set(pipeline.steps.map(s => s.type))] as type}
                      <span class="step-type-badge">{type}</span>
                    {/each}
                  </div>
                </div>
                <div class="step-preview">
                  {#each pipeline.steps.slice(0, 4) as step, i}
                    <span class="step-chip" title={step.name}>
                      {i + 1}. {step.name}
                    </span>
                  {/each}
                  {#if pipeline.steps.length > 4}
                    <span class="step-chip more">+{pipeline.steps.length - 4} more</span>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Platform pipelines -->
        {#if $pipelinesStore.length > 0 || repoPipelines.length > 0}
          <div class="section-header">
            <h2>Platform Pipelines</h2>
          </div>
        {/if}

        {#if $pipelinesStore.length === 0 && repoPipelines.length === 0}
          <div class="empty-state">
            <span class="empty-icon">📋</span>
            <p>No pipelines yet</p>
            <button class="btn-primary" on:click={handleCreate}>Create your first pipeline</button>
          </div>
        {:else if $pipelinesStore.length === 0}
          <p class="empty-section">No platform pipelines. <button class="btn-link" on:click={handleCreate}>Create one</button></p>
        {:else}
          <div class="pipelines-grid">
            {#each $pipelinesStore as pipeline}
              <div class="pipeline-card">
                <div class="card-header">
                  <h3>{pipeline.name}</h3>
                  <div class="card-actions">
                    <button class="btn-icon" title="Edit" on:click={() => handleEdit(pipeline)}>
                      <span>Edit</span>
                    </button>
                    <button class="btn-run" title="Run" on:click={() => handleRun(pipeline)}>
                      Run
                    </button>
                  </div>
                </div>
                {#if pipeline.description}
                  <p class="card-description">{pipeline.description}</p>
                {/if}
                <div class="card-meta">
                  <span class="step-count">{pipeline.steps.length} steps</span>
                  <div class="step-types">
                    {#each [...new Set(pipeline.steps.map(s => s.type))] as type}
                      <span class="step-type-badge">{type}</span>
                    {/each}
                  </div>
                </div>
                <div class="step-preview">
                  {#each pipeline.steps.slice(0, 4) as step, i}
                    <span class="step-chip" title={step.name}>
                      {i + 1}. {step.name}
                    </span>
                  {/each}
                  {#if pipeline.steps.length > 4}
                    <span class="step-chip more">+{pipeline.steps.length - 4} more</span>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}
      {:else}
        {#if allRuns.length === 0}
          <div class="empty-state">
            <span class="empty-icon">⟳</span>
            <p>No pipeline runs yet</p>
          </div>
        {:else}
          <div class="runs-table-container">
            <table class="runs-table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Progress</th>
                  <th>Trigger</th>
                  <th>Started</th>
                  <th>Duration</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {#each allRuns as run}
                  <tr class="run-row" on:click={() => handleViewRun(run)}>
                    <td>
                      <span class="status-badge" style="color: {getStatusColor(run.status as RunStatus)}">
                        {getStatusIcon(run.status as RunStatus)}
                        <span>{run.status}</span>
                      </span>
                    </td>
                    <td>
                      <div class="progress-cell">
                        <div class="step-badges">
                          {#each run.step_runs || [] as stepRun, i}
                            <span
                              class="step-badge"
                              style="background: {getStatusColor(stepRun.status as RunStatus)}"
                              title="{stepRun.step_name}: {stepRun.status}"
                            ></span>
                          {/each}
                          {#if !run.step_runs || run.step_runs.length === 0}
                            {#each Array(run.steps_total) as _, i}
                              <span class="step-badge pending"></span>
                            {/each}
                          {/if}
                        </div>
                        <span class="progress-text-mini">{run.steps_completed}/{run.steps_total}</span>
                      </div>
                    </td>
                    <td>
                      <span class="trigger-badge">{run.trigger_type}</span>
                    </td>
                    <td>{formatDate(run.created_at)}</td>
                    <td>{formatDuration(run.started_at, run.completed_at)}</td>
                    <td>
                      <button class="btn-view">View</button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
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
  .pipelines-page {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding: 1.5rem 2rem;
  }

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.5rem;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .page-header h1 {
    margin: 0;
    font-size: 1.75rem;
    font-weight: 700;
    color: var(--text-color);
  }

  .repo-badge {
    padding: 0.25rem 0.75rem;
    background: var(--primary-color);
    color: var(--primary-text);
    border-radius: 20px;
    font-size: 0.85rem;
    font-weight: 500;
  }

  .section-header {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin-bottom: 1rem;
    margin-top: 1.5rem;
  }

  .section-header:first-child {
    margin-top: 0;
  }

  .section-header h2 {
    margin: 0;
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .section-hint {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-family: monospace;
  }

  .repo-card {
    border-left: 3px solid var(--primary-color);
  }

  .repo-source-badge {
    font-size: 0.65rem;
    padding: 0.15rem 0.4rem;
    background: var(--primary-color);
    color: var(--primary-text);
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    margin-left: 0.5rem;
    vertical-align: middle;
  }

  .read-only-hint {
    opacity: 0.6;
    cursor: help;
    font-size: 1.1rem;
  }

  .empty-section {
    color: var(--text-muted);
    font-size: 0.9rem;
    margin: 0.5rem 0 1.5rem;
  }

  .btn-link {
    background: none;
    border: none;
    color: var(--primary-color);
    cursor: pointer;
    text-decoration: underline;
    font-size: inherit;
    padding: 0;
  }

  .btn-link:hover {
    opacity: 0.8;
  }

  .btn-primary {
    padding: 0.6rem 1.25rem;
    background: var(--primary-color);
    color: var(--primary-text);
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
  }

  .btn-primary:hover {
    opacity: 0.9;
  }

  .empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1rem;
    color: var(--text-muted);
    padding: 3rem;
  }

  .empty-icon {
    font-size: 3rem;
    opacity: 0.5;
  }

  .empty-state p {
    margin: 0;
    font-size: 1.1rem;
  }

  .tabs {
    display: flex;
    gap: 0.5rem;
    border-bottom: 1px solid var(--border-color);
    margin-bottom: 1.5rem;
  }

  .tab {
    position: relative;
    padding: 0.75rem 1.25rem;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font-size: 0.95rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .tab:hover {
    color: var(--text-color);
  }

  .tab.active {
    color: var(--primary-color);
    border-bottom-color: var(--primary-color);
  }

  .active-indicator {
    width: 8px;
    height: 8px;
    background: var(--warning-color);
    border-radius: 50%;
    animation: pulse 1.5s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  .tab-content {
    flex: 1;
    overflow-y: auto;
  }

  .pipelines-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
    gap: 1.25rem;
  }

  .pipeline-card {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .pipeline-card:hover {
    border-color: var(--primary-color);
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
  }

  .card-header h3 {
    margin: 0;
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .card-actions {
    display: flex;
    gap: 0.5rem;
  }

  .btn-icon {
    padding: 0.4rem 0.75rem;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-muted);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .btn-icon:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .btn-run {
    padding: 0.4rem 0.75rem;
    background: var(--success-color);
    color: var(--surface-color);
    border: none;
    border-radius: 4px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
  }

  .btn-run:hover {
    opacity: 0.9;
  }

  .card-description {
    margin: 0;
    font-size: 0.9rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  .card-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .step-count {
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .step-types {
    display: flex;
    gap: 0.4rem;
  }

  .step-type-badge {
    padding: 0.2rem 0.5rem;
    background: var(--surface-alt);
    border-radius: 4px;
    font-size: 0.75rem;
    color: var(--text-muted);
    text-transform: uppercase;
  }

  .step-preview {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    padding-top: 0.5rem;
    border-top: 1px dashed var(--border-color);
  }

  .step-chip {
    padding: 0.25rem 0.5rem;
    background: var(--surface-alt);
    border-radius: 4px;
    font-size: 0.8rem;
    color: var(--text-muted);
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .step-chip.more {
    font-style: italic;
    color: var(--primary-color);
  }

  .runs-table-container {
    overflow-x: auto;
  }

  .runs-table {
    width: 100%;
    border-collapse: collapse;
  }

  .runs-table th {
    text-align: left;
    padding: 0.75rem 1rem;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border-color);
  }

  .runs-table td {
    padding: 1rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .run-row {
    cursor: pointer;
  }

  .run-row:hover td {
    background: var(--surface-color);
  }

  .status-badge {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 500;
    text-transform: uppercase;
    font-size: 0.85rem;
  }

  .progress-cell {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .step-badges {
    display: flex;
    gap: 3px;
    align-items: center;
  }

  .step-badge {
    width: 12px;
    height: 12px;
    border-radius: 3px;
    flex-shrink: 0;
  }

  .step-badge.pending {
    background: var(--text-muted);
    opacity: 0.3;
  }

  .progress-bar-mini {
    width: 80px;
    height: 6px;
    background: var(--surface-alt);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-fill-mini {
    height: 100%;
    background: var(--primary-color);
    transition: width 0.3s ease;
  }

  .progress-fill-mini.running {
    background: var(--warning-color);
  }

  .progress-fill-mini.passed {
    background: var(--success-color);
  }

  .progress-fill-mini.failed {
    background: var(--error-color);
  }

  .progress-text-mini {
    font-family: monospace;
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .trigger-badge {
    padding: 0.2rem 0.5rem;
    background: var(--surface-alt);
    border-radius: 4px;
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .btn-view {
    padding: 0.35rem 0.75rem;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-muted);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .btn-view:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }
</style>
