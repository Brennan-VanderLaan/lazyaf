<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { playgroundStore, isRunning, canStart, hasResult } from '../stores/playground';
  import { modelsStore, claudeModels, geminiModels, modelsLoading } from '../stores/models';
  import { selectedRepoId, selectedRepo } from '../stores/repos';
  import { agentFilesStore } from '../stores/agentFiles';
  import { repos, lazyafFiles } from '../api/client';
  import type { BranchInfo, AgentFile, RepoAgent, MergedAgent } from '../api/types';
  import RawDiffViewer from '../components/RawDiffViewer.svelte';

  let branches: BranchInfo[] = [];
  let branchesLoading = false;
  let repoAgents: RepoAgent[] = [];
  let repoAgentsLoading = false;

  // UI refs
  let logsContainer: HTMLDivElement;
  let autoScroll = true;
  let scrollTimeout: ReturnType<typeof setTimeout> | null = null;

  // Timer for duration display
  let elapsedSeconds = 0;
  let timerInterval: ReturnType<typeof setInterval> | null = null;

  // Start/stop timer based on running state
  $: if ($isRunning && !timerInterval) {
    elapsedSeconds = 0;
    timerInterval = setInterval(() => {
      elapsedSeconds++;
    }, 1000);
  } else if (!$isRunning && timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
    // Calculate final elapsed time from timestamps
    if ($playgroundStore.startedAt && $playgroundStore.completedAt) {
      elapsedSeconds = Math.floor(
        ($playgroundStore.completedAt.getTime() - $playgroundStore.startedAt.getTime()) / 1000
      );
    }
  }

  // Load branches when repo changes
  $: if ($selectedRepoId) {
    loadBranches($selectedRepoId);
    loadRepoAgents($selectedRepoId);
    agentFilesStore.load();
  }

  // Get available models based on runner type
  $: availableModels = $playgroundStore.runnerType === 'claude-code' ? $claudeModels : $geminiModels;

  // Auto-select first model when runner type changes or models load
  $: {
    const models = $playgroundStore.runnerType === 'claude-code' ? $claudeModels : $geminiModels;
    const currentModel = $playgroundStore.model;
    if (models.length > 0) {
      const isValidModel = models.some(m => m.id === currentModel);
      if (!isValidModel) {
        playgroundStore.setConfig({ model: models[0].id as any });
      }
    }
  }

  // Auto-scroll logs (debounced to prevent UI lockup)
  $: if ($playgroundStore.logs.length && autoScroll && logsContainer) {
    if (scrollTimeout) clearTimeout(scrollTimeout);
    scrollTimeout = setTimeout(() => {
      if (logsContainer) {
        logsContainer.scrollTop = logsContainer.scrollHeight;
      }
    }, 100);
  }

  async function loadBranches(repoId: string) {
    branchesLoading = true;
    try {
      const response = await repos.branches(repoId);
      branches = response.branches;
      // Set default branch if not already set
      if (!$playgroundStore.branch && response.default_branch) {
        playgroundStore.setConfig({ branch: response.default_branch });
      }
    } catch (e) {
      console.error('Failed to load branches:', e);
      branches = [];
    } finally {
      branchesLoading = false;
    }
  }

  async function loadRepoAgents(repoId: string) {
    repoAgentsLoading = true;
    try {
      repoAgents = await lazyafFiles.listAgents(repoId);
    } catch {
      repoAgents = [];
    } finally {
      repoAgentsLoading = false;
    }
  }

  // Merge platform and repo agents for display
  $: mergedAgents = [
    ...$agentFilesStore.map((a): MergedAgent => ({
      id: a.id,
      name: a.name,
      description: a.description,
      content: a.content,
      source: 'platform',
    })),
    ...repoAgents.map((a): MergedAgent => ({
      name: a.name,
      description: a.description,
      prompt_template: a.prompt_template,
      source: 'repo',
    })),
  ];

  function handleAgentChange(event: Event) {
    const select = event.target as HTMLSelectElement;
    const value = select.value;

    if (!value) {
      playgroundStore.setConfig({ agentId: null, repoAgentName: null });
    } else if (value.startsWith('platform:')) {
      playgroundStore.setConfig({ agentId: value.slice(9), repoAgentName: null });
    } else if (value.startsWith('repo:')) {
      playgroundStore.setConfig({ agentId: null, repoAgentName: value.slice(5) });
    }
  }

  function getCurrentAgentValue(): string {
    if ($playgroundStore.agentId) {
      return `platform:${$playgroundStore.agentId}`;
    }
    if ($playgroundStore.repoAgentName) {
      return `repo:${$playgroundStore.repoAgentName}`;
    }
    return '';
  }

  async function handleStartTest() {
    try {
      await playgroundStore.startTest();
    } catch (e) {
      // Error is handled in store
    }
  }

  function handleCancel() {
    playgroundStore.cancel();
  }

  function handleReset() {
    playgroundStore.reset();
    // Keep the config but clear results
    if ($selectedRepoId) {
      playgroundStore.setConfig({ repoId: $selectedRepoId });
    }
  }

  function handleLogsScroll() {
    if (!logsContainer) return;
    const { scrollTop, scrollHeight, clientHeight } = logsContainer;
    // Auto-scroll if user is near the bottom
    autoScroll = scrollHeight - scrollTop - clientHeight < 100;
  }

  function getStatusColor(status: string): string {
    switch (status) {
      case 'queued': return 'var(--text-muted)';
      case 'running': return 'var(--warning-color)';
      case 'completed': return 'var(--success-color)';
      case 'failed': return 'var(--error-color)';
      case 'cancelled': return 'var(--text-muted)';
      default: return 'var(--text-muted)';
    }
  }

  function getStatusIcon(status: string): string {
    switch (status) {
      case 'idle': return '';
      case 'queued': return 'Queued';
      case 'running': return 'Running';
      case 'completed': return 'Completed';
      case 'failed': return 'Failed';
      case 'cancelled': return 'Cancelled';
      default: return status;
    }
  }

  function formatDuration(): string {
    let seconds = elapsedSeconds;

    // If completed, calculate from timestamps (more accurate)
    if ($playgroundStore.completedAt && $playgroundStore.startedAt) {
      seconds = Math.floor(
        ($playgroundStore.completedAt.getTime() - $playgroundStore.startedAt.getTime()) / 1000
      );
    } else if (!$isRunning && seconds === 0) {
      // Not running and no elapsed time tracked
      return '';
    }

    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${seconds % 60}s`;
  }

  // Set repo ID when selected repo changes
  $: if ($selectedRepoId && $selectedRepoId !== $playgroundStore.repoId) {
    playgroundStore.setConfig({ repoId: $selectedRepoId });
  }

  onMount(() => {
    // Load available models
    modelsStore.load();

    if ($selectedRepoId) {
      playgroundStore.setConfig({ repoId: $selectedRepoId });
    }
  });

  onDestroy(() => {
    // Clean up SSE connection if still running
    if ($isRunning) {
      playgroundStore.cancel();
    }
    // Clean up timer
    if (timerInterval) {
      clearInterval(timerInterval);
    }
    if (scrollTimeout) {
      clearTimeout(scrollTimeout);
    }
  });
</script>

<div class="playground-page">
  <header class="page-header">
    <div class="header-left">
      <h1>Agent Playground</h1>
      {#if $selectedRepo}
        <span class="repo-badge">{$selectedRepo.name}</span>
      {/if}
    </div>
    {#if $playgroundStore.status !== 'idle'}
      <div class="status-badge" style="color: {getStatusColor($playgroundStore.status)}">
        {#if $playgroundStore.status === 'running'}
          <span class="spinner"></span>
        {/if}
        {getStatusIcon($playgroundStore.status)}
        {#if formatDuration()}
          <span class="duration">({formatDuration()})</span>
        {/if}
      </div>
    {/if}
  </header>

  {#if !$selectedRepoId}
    <div class="empty-state">
      <p>Select a repository from the sidebar to use the Agent Playground.</p>
    </div>
  {:else}
    <div class="playground-layout">
      <!-- Configuration Panel -->
      <aside class="config-panel">
        <h2>Configuration</h2>

        <div class="form-group">
          <label for="branch">Branch</label>
          <select
            id="branch"
            value={$playgroundStore.branch || ''}
            on:change={(e) => playgroundStore.setConfig({ branch: e.currentTarget.value })}
            disabled={$isRunning}
          >
            <option value="">Select branch...</option>
            {#each branches as branch}
              <option value={branch.name}>
                {branch.name} {branch.is_default ? '(default)' : ''}
              </option>
            {/each}
          </select>
          {#if branchesLoading}
            <span class="loading-hint">Loading branches...</span>
          {/if}
        </div>

        <div class="form-group">
          <label for="agent">Agent (optional)</label>
          <select
            id="agent"
            value={getCurrentAgentValue()}
            on:change={handleAgentChange}
            disabled={$isRunning}
          >
            <option value="">No agent (use task only)</option>
            {#if mergedAgents.filter(a => a.source === 'platform').length > 0}
              <optgroup label="Platform Agents">
                {#each mergedAgents.filter(a => a.source === 'platform') as agent}
                  <option value="platform:{agent.id}">{agent.name}</option>
                {/each}
              </optgroup>
            {/if}
            {#if mergedAgents.filter(a => a.source === 'repo').length > 0}
              <optgroup label="Repo Agents">
                {#each mergedAgents.filter(a => a.source === 'repo') as agent}
                  <option value="repo:{agent.name}">{agent.name}</option>
                {/each}
              </optgroup>
            {/if}
          </select>
        </div>

        <div class="form-row">
          <div class="form-group half">
            <label for="runner-type">Runner Type</label>
            <select
              id="runner-type"
              value={$playgroundStore.runnerType}
              on:change={(e) => playgroundStore.setConfig({ runnerType: e.currentTarget.value as 'claude-code' | 'gemini' })}
              disabled={$isRunning}
            >
              <option value="claude-code">Claude Code</option>
              <option value="gemini">Gemini</option>
            </select>
          </div>

          <div class="form-group half">
            <label for="model">Model {#if $modelsLoading}<span class="loading-indicator">(loading...)</span>{/if}</label>
            <select
              id="model"
              value={$playgroundStore.model}
              on:change={(e) => playgroundStore.setConfig({ model: e.currentTarget.value as any })}
              disabled={$isRunning || $modelsLoading || availableModels.length === 0}
            >
              {#each availableModels as model}
                <option value={model.id} title={model.description}>{model.name}</option>
              {/each}
            </select>
          </div>
        </div>

        <div class="form-group">
          <label for="task">Task Description</label>
          <textarea
            id="task"
            placeholder="Describe what you want the agent to do..."
            value={$playgroundStore.taskOverride}
            on:input={(e) => playgroundStore.setConfig({ taskOverride: e.currentTarget.value })}
            disabled={$isRunning}
            rows="4"
          ></textarea>
        </div>

        <div class="form-group checkbox-group">
          <label>
            <input
              type="checkbox"
              checked={$playgroundStore.saveToBranch}
              on:change={(e) => playgroundStore.setConfig({ saveToBranch: e.currentTarget.checked })}
              disabled={$isRunning}
            />
            Save changes to branch
          </label>
          {#if $playgroundStore.saveToBranch}
            <input
              type="text"
              placeholder="Branch name (e.g., playground/test-1)"
              value={$playgroundStore.saveBranchName}
              on:input={(e) => playgroundStore.setConfig({ saveBranchName: e.currentTarget.value })}
              disabled={$isRunning}
              class="branch-name-input"
            />
          {/if}
        </div>

        <div class="button-group">
          {#if $canStart}
            <button
              class="btn-primary"
              on:click={handleStartTest}
              disabled={!$playgroundStore.branch || !$playgroundStore.taskOverride}
            >
              Test Once
            </button>
          {:else if $isRunning}
            <button class="btn-danger" on:click={handleCancel}>
              Cancel
            </button>
          {/if}

          {#if $hasResult}
            <button class="btn-secondary" on:click={handleReset}>
              Reset
            </button>
          {/if}
        </div>

        {#if $playgroundStore.error}
          <div class="error-message">
            {$playgroundStore.error}
          </div>
        {/if}
      </aside>

      <!-- Output Panel -->
      <main class="output-panel">
        <!-- Logs Section -->
        <section class="logs-section">
          <div class="section-header">
            <h3>Agent Output</h3>
            {#if $playgroundStore.logs.length > 0}
              <button class="btn-small" on:click={() => playgroundStore.clearLogs()}>
                Clear
              </button>
            {/if}
          </div>
          <div
            class="logs-container"
            bind:this={logsContainer}
            on:scroll={handleLogsScroll}
          >
            {#if $playgroundStore.logs.length === 0}
              <div class="logs-empty">
                {#if $isRunning}
                  <span class="spinner"></span>
                  <span>Waiting for output...</span>
                {:else}
                  <span>Agent output will appear here when you run a test.</span>
                {/if}
              </div>
            {:else}
              {#each $playgroundStore.logs as log}
                <div class="log-line">{log}</div>
              {/each}
            {/if}
          </div>
        </section>

        <!-- Diff Section -->
        {#if $hasResult && ($playgroundStore.diff || $playgroundStore.filesChanged.length > 0)}
          <section class="diff-section">
            <div class="section-header">
              <h3>Changes</h3>
            </div>
            <RawDiffViewer
              diff={$playgroundStore.diff || ''}
              filesChanged={$playgroundStore.filesChanged}
            />
          </section>
        {:else if $hasResult && !$playgroundStore.diff && !$playgroundStore.error && $playgroundStore.filesChanged.length === 0}
          <section class="diff-section">
            <div class="section-header">
              <h3>Changes</h3>
            </div>
            <div class="diff-empty">
              No changes were made by the agent.
            </div>
          </section>
        {/if}
      </main>
    </div>
  {/if}
</div>

<style>
  .playground-page {
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 1rem;
    gap: 1rem;
  }

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .header-left h1 {
    margin: 0;
    font-size: 1.5rem;
    color: var(--text-color);
  }

  .repo-badge {
    background: var(--badge-bg);
    padding: 0.25rem 0.75rem;
    border-radius: 1rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .status-badge {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 500;
  }

  .duration {
    color: var(--text-muted);
    font-weight: normal;
  }

  .empty-state {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1;
    color: var(--text-muted);
  }

  .playground-layout {
    display: flex;
    gap: 1rem;
    flex: 1;
    min-height: 0;
  }

  .config-panel {
    width: 320px;
    min-width: 280px;
    background: var(--surface-color);
    border-radius: 8px;
    padding: 1rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
    overflow-y: auto;
  }

  .config-panel h2 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--text-color);
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .form-row {
    display: flex;
    gap: 0.75rem;
  }

  .form-group.half {
    flex: 1;
  }

  .form-group label {
    font-size: 0.9rem;
    color: var(--text-muted);
  }

  .loading-indicator {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-style: italic;
  }

  .form-group select,
  .form-group input[type="text"],
  .form-group textarea {
    background: var(--input-bg);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 0.5rem 0.75rem;
    color: var(--text-color);
    font-size: 0.9rem;
  }

  .form-group select:focus,
  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .form-group select:disabled,
  .form-group input:disabled,
  .form-group textarea:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .form-group textarea {
    resize: vertical;
    min-height: 80px;
  }

  .checkbox-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    color: var(--text-color);
  }

  .checkbox-group input[type="checkbox"] {
    width: 16px;
    height: 16px;
    accent-color: var(--primary-color);
  }

  .branch-name-input {
    margin-top: 0.5rem;
  }

  .loading-hint {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-style: italic;
  }

  .button-group {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .btn-primary,
  .btn-secondary,
  .btn-danger,
  .btn-small {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: all 0.2s;
  }

  .btn-primary {
    background: var(--primary-color);
    color: var(--primary-text);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-secondary {
    background: var(--badge-bg);
    color: var(--text-color);
  }

  .btn-secondary:hover {
    background: var(--hover-color);
  }

  .btn-danger {
    background: var(--error-color);
    color: white;
  }

  .btn-danger:hover {
    filter: brightness(1.1);
  }

  .btn-small {
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    background: var(--badge-bg);
    color: var(--text-muted);
  }

  .btn-small:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .error-message {
    padding: 0.75rem;
    background: rgba(243, 139, 168, 0.1);
    border: 1px solid var(--error-color);
    border-radius: 6px;
    color: var(--error-color);
    font-size: 0.9rem;
  }

  .output-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 1rem;
    min-width: 0;
  }

  .logs-section,
  .diff-section {
    background: var(--surface-color);
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .logs-section {
    flex: 1;
    min-height: 150px;
  }

  .diff-section {
    flex: 2;
    min-height: 300px;
    overflow-y: auto;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border-color);
  }

  .section-header h3 {
    margin: 0;
    font-size: 1rem;
    color: var(--text-color);
  }

  .files-count {
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .logs-container {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem;
    font-family: 'Fira Code', 'Monaco', 'Consolas', monospace;
    font-size: 0.85rem;
    line-height: 1.5;
  }

  .logs-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    height: 100%;
    color: var(--text-muted);
    font-style: italic;
  }

  .log-line {
    white-space: pre-wrap;
    word-break: break-all;
    padding: 0.1rem 0.5rem;
    color: var(--text-color);
  }

  .log-line:hover {
    background: var(--hover-color);
  }

  .diff-empty {
    padding: 1rem;
    color: var(--text-muted);
    font-style: italic;
    text-align: center;
  }

  .spinner {
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid var(--border-color);
    border-top-color: var(--primary-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
