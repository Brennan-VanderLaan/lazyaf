<script lang="ts">
  import { createEventDispatcher, onMount, tick } from 'svelte';
  import type { Card, CardStatus, BranchInfo, MergeResult, RebaseResult, RunnerType, StepType, StepConfig, AgentFile, RepoAgent, MergedAgent } from '../api/types';
  import { cardsStore } from '../stores/cards';
  import { selectedRepo } from '../stores/repos';
  import { agentFilesStore } from '../stores/agentFiles';
  import { repos, lazyafFiles } from '../api/client';
  import JobStatus from './JobStatus.svelte';
  import DiffViewer from './DiffViewer.svelte';
  import ConflictResolver from './ConflictResolver.svelte';

  export let repoId: string;
  export let card: Card | null = null;

  // Branch state
  let branches: BranchInfo[] = [];
  let loadingBranches = false;
  let selectedTargetBranch: string = '';
  let mergeResult: MergeResult | null = null;
  let rebaseResult: RebaseResult | null = null;
  let diffRefreshKey: number = 0;  // Increment to force diff reload

  $: showDiff = card?.branch_name && (card?.status === 'in_review' || card?.status === 'done' || card?.status === 'failed');
  $: baseBranch = selectedTargetBranch || $selectedRepo?.default_branch || 'main';
  $: nonCardBranches = branches.filter(b => b.name !== card?.branch_name);

  const dispatch = createEventDispatcher<{
    close: void;
    created: Card;
    updated: Card;
    deleted: void;
  }>();

  let title = card?.title ?? '';
  let description = card?.description ?? '';
  let runnerType: RunnerType = card?.runner_type ?? 'any';
  let stepType: StepType = card?.step_type ?? 'agent';
  let stepCommand: string = card?.step_config?.command ?? '';
  let stepImage: string = card?.step_config?.image ?? '';
  let stepWorkingDir: string = card?.step_config?.working_dir ?? '';
  let promptTemplate: string = card?.prompt_template ?? '';
  let selectedAgentFileIds: string[] = card?.agent_file_ids ?? [];
  let showPromptTemplate = false;  // Toggle for advanced options
  let submitting = false;

  // Agent files - merge platform and repo agents
  let repoAgents: RepoAgent[] = [];

  // Load repo agents when repo changes
  async function loadRepoAgents() {
    if ($selectedRepo?.is_ingested) {
      try {
        repoAgents = await lazyafFiles.listAgents($selectedRepo.id);
      } catch {
        repoAgents = [];
      }
    } else {
      repoAgents = [];
    }
  }

  // Merge platform + repo agents (repo overrides platform by name)
  $: mergedAgents = (() => {
    const agentsByName = new Map<string, MergedAgent>();

    // Platform agents first
    for (const agent of $agentFilesStore) {
      agentsByName.set(agent.name, {
        id: agent.id,
        name: agent.name,
        description: agent.description ?? null,
        content: agent.content,
        source: 'platform',
      });
    }

    // Repo agents override platform
    for (const agent of repoAgents) {
      agentsByName.set(agent.name, {
        name: agent.name,
        description: agent.description,
        prompt_template: agent.prompt_template,
        source: 'repo',
      });
    }

    return Array.from(agentsByName.values());
  })();

  // Normalize agent name to CLI-safe format (same as AgentFileModal)
  function normalizeAgentName(input: string): string {
    return input
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9-]/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '');
  }

  const runnerTypeOptions: { value: RunnerType; label: string }[] = [
    { value: 'any', label: 'Any Runner' },
    { value: 'claude-code', label: 'Claude Code' },
    { value: 'gemini', label: 'Gemini CLI' },
  ];

  const stepTypeOptions: { value: StepType; label: string; description: string }[] = [
    { value: 'agent', label: 'AI Agent', description: 'AI implements the feature using Claude or Gemini' },
    { value: 'script', label: 'Shell Script', description: 'Run a shell command directly in the repo' },
    { value: 'docker', label: 'Docker Container', description: 'Run a command inside a Docker container' },
  ];

  // Build step config from individual fields
  function buildStepConfig(): StepConfig | null {
    if (stepType === 'agent') return null;

    const config: StepConfig = {};
    if (stepCommand) config.command = stepCommand;
    if (stepImage && stepType === 'docker') config.image = stepImage;
    if (stepWorkingDir && stepType === 'script') config.working_dir = stepWorkingDir;

    return Object.keys(config).length > 0 ? config : null;
  }

  $: isEdit = card !== null;
  $: canStart = card?.status === 'todo';
  $: canApprove = card?.status === 'in_review';
  $: canReject = card?.status === 'in_review' || card?.status === 'in_progress';
  $: canRetry = card?.status === 'failed' || card?.status === 'in_review';
  $: canRebase = card?.branch_name && (card?.status === 'in_progress' || card?.status === 'in_review');

  onMount(async () => {
    if ($selectedRepo?.is_ingested) {
      loadBranches();
    }
    // Load agent files for multi-select
    agentFilesStore.load();
    // Load repo-defined agents
    loadRepoAgents();
  });

  async function loadBranches() {
    loadingBranches = true;
    try {
      const response = await repos.branches(repoId);
      branches = response.branches;
      // Set default target branch
      const defaultBranch = branches.find(b => b.is_default);
      if (defaultBranch) {
        selectedTargetBranch = defaultBranch.name;
      }
    } catch (e) {
      console.error('Failed to load branches:', e);
    } finally {
      loadingBranches = false;
    }
  }

  async function handleSubmit() {
    if (!title.trim()) return;
    submitting = true;

    try {
      const stepConfig = buildStepConfig();
      // Only include prompt_template and agent_file_ids for agent step type
      const agentPrompt = stepType === 'agent' && promptTemplate.trim() ? promptTemplate : null;
      const agentFiles = stepType === 'agent' && selectedAgentFileIds.length > 0 ? selectedAgentFileIds : null;

      if (isEdit && card) {
        const updated = await cardsStore.update(card.id, {
          title,
          description,
          runner_type: runnerType,
          step_type: stepType,
          step_config: stepConfig,
          prompt_template: agentPrompt,
          agent_file_ids: agentFiles,
        });
        dispatch('updated', updated);
      } else {
        const created = await cardsStore.create(repoId, {
          title,
          description,
          runner_type: runnerType,
          step_type: stepType,
          step_config: stepConfig,
          prompt_template: agentPrompt,
          agent_file_ids: agentFiles,
        });
        // Wait for Svelte to apply all pending state changes before dispatching
        // This prevents race conditions with WebSocket updates that might interfere with modal closure
        await tick();
        dispatch('created', created);
      }
    } catch (e) {
      // Show error but don't close modal so user can retry
      alert(e instanceof Error ? e.message : 'Failed to save card');
    } finally {
      submitting = false;
    }
  }

  async function handleDelete() {
    if (!card || !confirm('Are you sure you want to delete this card?')) return;
    submitting = true;
    try {
      await cardsStore.delete(card.id);
      submitting = false;
      // Wait for Svelte to apply state changes before dispatching
      await tick();
      dispatch('deleted');
    } catch (e) {
      submitting = false;
      alert(e instanceof Error ? e.message : 'Failed to delete card');
    }
  }

  async function handleStart() {
    if (!card) return;
    submitting = true;
    try {
      const updated = await cardsStore.start(card.id);
      submitting = false;
      // Wait for Svelte to apply state changes before dispatching
      await tick();
      dispatch('updated', updated);
    } catch (e) {
      submitting = false;
      alert(e instanceof Error ? e.message : 'Failed to start card');
    }
  }

  async function handleApprove() {
    if (!card) return;
    submitting = true;
    try {
      const response = await cardsStore.approve(card.id, selectedTargetBranch || undefined);
      mergeResult = response.merge_result;

      // Only close modal if merge succeeded
      // If there are conflicts or errors, keep modal open to show them
      if (response.merge_result?.success !== false) {
        dispatch('updated', response.card);
      }
    } catch (e) {
      // Show error but don't close modal
      alert(e instanceof Error ? e.message : 'Merge failed');
    } finally {
      submitting = false;
    }
  }

  async function handleReject() {
    if (!card) return;
    submitting = true;
    try {
      const updated = await cardsStore.reject(card.id);
      submitting = false;
      // Wait for Svelte to apply state changes before dispatching
      await tick();
      dispatch('updated', updated);
    } catch (e) {
      submitting = false;
      alert(e instanceof Error ? e.message : 'Failed to reject card');
    }
  }

  async function handleRetry() {
    if (!card) return;
    submitting = true;
    try {
      const updated = await cardsStore.retry(card.id);
      submitting = false;
      // Wait for Svelte to apply state changes before dispatching
      await tick();
      dispatch('updated', updated);
    } catch (e) {
      submitting = false;
      alert(e instanceof Error ? e.message : 'Failed to retry card');
    }
  }

  async function handleRebase() {
    if (!card) return;
    submitting = true;
    rebaseResult = null;
    try {
      const response = await cardsStore.rebase(card.id, selectedTargetBranch || undefined);
      rebaseResult = response.rebase_result;
      // Force diff to reload after rebase
      diffRefreshKey++;
      // Only close modal if rebase succeeded without conflicts
      if (rebaseResult?.success && !rebaseResult?.conflicts?.length) {
        dispatch('updated', response.card);
      }
    } catch (e) {
      // Show error but don't close modal
      alert(e instanceof Error ? e.message : 'Rebase failed');
    } finally {
      submitting = false;
    }
  }

  async function handleResolveConflicts(resolutions: Array<{ path: string; content: string }>) {
    if (!card) return;
    submitting = true;
    try {
      const response = await cardsStore.resolveConflicts(card.id, selectedTargetBranch || undefined, resolutions);
      mergeResult = response.merge_result;
      dispatch('updated', response.card);
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Conflict resolution failed');
    } finally {
      submitting = false;
    }
  }

  function handleCancelConflictResolution() {
    // Reset merge result to clear conflicts
    mergeResult = null;
  }

  async function handleResolveRebaseConflicts(resolutions: Array<{ path: string; content: string }>) {
    if (!card) return;
    submitting = true;
    try {
      const response = await cardsStore.resolveRebaseConflicts(card.id, selectedTargetBranch || undefined, resolutions);
      rebaseResult = response.rebase_result;
      // Force diff to reload after conflict resolution
      diffRefreshKey++;
      dispatch('updated', response.card);
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Conflict resolution failed');
    } finally {
      submitting = false;
    }
  }

  function handleCancelRebaseConflictResolution() {
    // Reset rebase result to clear conflicts
    rebaseResult = null;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      dispatch('close');
    }
  }

  function handleBackdropClick() {
    dispatch('close');
  }

  const statusLabels: Record<CardStatus, string> = {
    todo: 'To Do',
    in_progress: 'In Progress',
    in_review: 'In Review',
    done: 'Done',
    failed: 'Failed',
  };
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="modal-backdrop" on:click={handleBackdropClick} role="dialog" aria-modal="true">
  <div class="modal" on:click|stopPropagation role="document">
    <div class="modal-header">
      <h2>{isEdit ? 'Edit Card' : 'New Card'}</h2>
      <button class="btn-close" on:click={() => dispatch('close')}>✕</button>
    </div>

    <form on:submit|preventDefault={handleSubmit}>
      <div class="form-group">
        <label for="title">Title</label>
        <input
          id="title"
          type="text"
          bind:value={title}
          placeholder="What needs to be done?"
          required
        />
      </div>

      <div class="form-group">
        <label for="description">Description</label>
        <textarea
          id="description"
          bind:value={description}
          placeholder="Describe the feature or task in detail..."
          rows="5"
        ></textarea>
      </div>

      {#if !isEdit || card?.status === 'todo'}
        <div class="form-group">
          <label for="step-type">Step Type</label>
          <div class="step-type-selector">
            {#each stepTypeOptions as option}
              <button
                type="button"
                class="step-type-option"
                class:selected={stepType === option.value}
                on:click={() => stepType = option.value}
              >
                <span class="step-type-label">{option.label}</span>
                <span class="step-type-desc">{option.description}</span>
              </button>
            {/each}
          </div>
        </div>

        {#if stepType === 'agent'}
          <div class="form-group">
            <label for="runner-type">Runner Type</label>
            <select id="runner-type" bind:value={runnerType}>
              {#each runnerTypeOptions as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
            <p class="form-hint">
              {#if runnerType === 'any'}
                First available runner will pick up this task.
              {:else if runnerType === 'claude-code'}
                Only Claude Code runners will work on this task.
              {:else if runnerType === 'gemini'}
                Only Gemini CLI runners will work on this task.
              {/if}
            </p>
          </div>

          {#if mergedAgents.length > 0}
            <div class="form-group">
              <label>Available Agents</label>
              <div class="agent-file-selector">
                {#each mergedAgents as agent}
                  {@const cliName = normalizeAgentName(agent.name) || agent.name}
                  {#if agent.source === 'platform' && agent.id}
                    <label class="agent-checkbox">
                      <input
                        type="checkbox"
                        bind:group={selectedAgentFileIds}
                        value={agent.id}
                      />
                      <span class="agent-name">@{cliName}</span>
                      {#if agent.description}
                        <span class="agent-desc">{agent.description}</span>
                      {/if}
                    </label>
                  {:else}
                    <div class="agent-checkbox repo-agent">
                      <span class="agent-name">@{cliName}</span>
                      <span class="agent-source-badge">from repo</span>
                      {#if agent.description}
                        <span class="agent-desc">{agent.description}</span>
                      {/if}
                    </div>
                  {/if}
                {/each}
              </div>
              <p class="form-hint">
                Platform agents can be selected. Repo agents are available via <code>@agent-name</code> syntax.
              </p>
            </div>
          {/if}

          <div class="form-group">
            <button
              type="button"
              class="btn-toggle-advanced"
              on:click={() => showPromptTemplate = !showPromptTemplate}
            >
              {showPromptTemplate ? '- Hide' : '+ Show'} Custom Prompt
            </button>
          </div>

          {#if showPromptTemplate}
            <div class="form-group">
              <label for="prompt-template">Custom Prompt Template</label>
              <textarea
                id="prompt-template"
                bind:value={promptTemplate}
                placeholder={'Override the default prompt. Use {{title}} and {{description}} as placeholders.'}
                rows="6"
              ></textarea>
              <p class="form-hint">
                Leave empty to use the default prompt. Placeholders: <code>{'{{title}}'}</code>, <code>{'{{description}}'}</code>
              </p>
            </div>
          {/if}
        {:else if stepType === 'script'}
          <div class="form-group">
            <label for="step-command">Script</label>
            <textarea
              id="step-command"
              class="script-input"
              bind:value={stepCommand}
              placeholder={'npm install\nnpm test'}
              rows="4"
              required
            ></textarea>
            <p class="form-hint">Shell script to run in the repository. Supports multi-line scripts.</p>
          </div>
          <div class="form-group">
            <label for="step-working-dir">Working Directory (optional)</label>
            <input
              id="step-working-dir"
              type="text"
              bind:value={stepWorkingDir}
              placeholder="Leave empty for repo root"
            />
            <p class="form-hint">Relative path from repo root.</p>
          </div>
        {:else if stepType === 'docker'}
          <div class="form-group">
            <label for="step-image">Docker Image</label>
            <input
              id="step-image"
              type="text"
              bind:value={stepImage}
              placeholder="node:20"
              required
            />
            <p class="form-hint">Docker image to run the command in.</p>
          </div>
          <div class="form-group">
            <label for="step-command">Command</label>
            <textarea
              id="step-command"
              class="script-input"
              bind:value={stepCommand}
              placeholder={'npm install\nnpm test'}
              rows="3"
              required
            ></textarea>
            <p class="form-hint">Command to run inside the container. Supports multi-line scripts.</p>
          </div>
        {/if}
      {/if}

      {#if isEdit && card}
        <div class="card-meta">
          <div class="meta-item">
            <span class="meta-label">Status:</span>
            <span class="meta-value status-badge" data-status={card.status}>
              {statusLabels[card.status]}
            </span>
          </div>
          <div class="meta-item">
            <span class="meta-label">Type:</span>
            <span class="meta-value step-type-badge" data-step={card.step_type}>
              {stepTypeOptions.find(o => o.value === card.step_type)?.label || card.step_type}
            </span>
          </div>
          {#if card.step_type !== 'agent' && card.step_config}
            {#if card.step_config.command}
              <div class="meta-item">
                <span class="meta-label">Command:</span>
                <code class="meta-value">{card.step_config.command}</code>
              </div>
            {/if}
            {#if card.step_config.image}
              <div class="meta-item">
                <span class="meta-label">Image:</span>
                <code class="meta-value">{card.step_config.image}</code>
              </div>
            {/if}
          {/if}
          {#if card.status !== 'todo' && card.step_type === 'agent'}
            <div class="meta-item">
              <span class="meta-label">Runner:</span>
              <span class="meta-value runner-type-badge" data-runner={card.completed_runner_type || card.runner_type}>
                {#if card.completed_runner_type}
                  {runnerTypeOptions.find(o => o.value === card.completed_runner_type)?.label || card.completed_runner_type}
                {:else}
                  {runnerTypeOptions.find(o => o.value === card.runner_type)?.label || card.runner_type}
                {/if}
              </span>
            </div>
          {/if}
          {#if card.branch_name}
            <div class="meta-item">
              <span class="meta-label">Branch:</span>
              <code class="meta-value">{card.branch_name}</code>
            </div>
          {/if}
          {#if card.pr_url}
            <div class="meta-item">
              <span class="meta-label">PR:</span>
              <a href={card.pr_url} target="_blank" rel="noopener" class="meta-value">
                View Pull Request
              </a>
            </div>
          {/if}
        </div>

        {#if card.job_id && (card.status === 'in_progress' || card.status === 'in_review' || card.status === 'failed')}
          <JobStatus cardId={card.id} jobId={card.job_id} />
        {/if}

        {#if showDiff && card.branch_name}
          <div class="diff-section">
            <div class="diff-header">
              <h3>Code Changes</h3>
              <div class="diff-header-actions">
                {#if nonCardBranches.length > 0 && (card.status === 'in_review' || card.status === 'in_progress')}
                  <div class="branch-selector">
                    <label for="target-branch">Target branch:</label>
                    <select id="target-branch" bind:value={selectedTargetBranch}>
                      {#each nonCardBranches as branch}
                        <option value={branch.name}>
                          {branch.name}
                          {branch.is_default ? ' (default)' : ''}
                        </option>
                      {/each}
                    </select>
                  </div>
                {/if}
                {#if canRebase}
                  <button
                    type="button"
                    class="btn-rebase"
                    on:click={handleRebase}
                    disabled={submitting}
                    title="Pull in latest changes from target branch"
                  >
                    Update Branch
                  </button>
                {/if}
              </div>
            </div>
            <DiffViewer {repoId} {baseBranch} headBranch={card.branch_name} refreshKey={diffRefreshKey} />
          </div>
        {/if}

        {#if rebaseResult}
          {#if rebaseResult.conflicts && rebaseResult.conflicts.length > 0}
            <ConflictResolver
              conflicts={rebaseResult.conflicts}
              onResolve={handleResolveRebaseConflicts}
              onCancel={handleCancelRebaseConflictResolution}
              operation="rebase"
              sourceBranch={card.branch_name || 'feature'}
              targetBranch={baseBranch}
            />
          {:else}
            <div class="rebase-result" class:success={rebaseResult.success}>
              <div class="result-icon">{rebaseResult.success ? '✓' : '✗'}</div>
              <div class="result-info">
                <div class="result-message">{rebaseResult.message}</div>
                {#if rebaseResult.new_sha}
                  <div class="result-sha">New commit: <code>{rebaseResult.new_sha.slice(0, 8)}</code></div>
                {/if}
              </div>
            </div>
          {/if}
        {/if}

        {#if mergeResult}
          {#if mergeResult.conflicts && mergeResult.conflicts.length > 0}
            <ConflictResolver
              conflicts={mergeResult.conflicts}
              onResolve={handleResolveConflicts}
              onCancel={handleCancelConflictResolution}
              operation="merge"
              sourceBranch={card?.branch_name || 'feature'}
              targetBranch={baseBranch}
            />
          {:else}
            <div class="merge-result" class:success={mergeResult.success}>
              <div class="merge-icon">{mergeResult.success ? '✓' : '✗'}</div>
              <div class="merge-info">
                <div class="merge-message">{mergeResult.message}</div>
                {#if mergeResult.merge_type}
                  <div class="merge-type">Type: {mergeResult.merge_type}</div>
                {/if}
                {#if mergeResult.new_sha}
                  <div class="merge-sha">New commit: <code>{mergeResult.new_sha.slice(0, 8)}</code></div>
                {/if}
              </div>
            </div>
          {/if}
        {/if}
      {/if}

      <div class="modal-actions">
        {#if isEdit && card}
          <div class="action-group-left">
            <button
              type="button"
              class="btn-delete"
              on:click={handleDelete}
              disabled={submitting}
            >
              Delete
            </button>
          </div>

          <div class="action-group-right">
            {#if canStart}
              <button
                type="button"
                class="btn-action btn-start"
                on:click={handleStart}
                disabled={submitting}
              >
                🚀 Start Work
              </button>
            {/if}
            {#if canApprove}
              <button
                type="button"
                class="btn-action btn-approve"
                on:click={handleApprove}
                disabled={submitting}
              >
                ✓ Approve
              </button>
            {/if}
            {#if canReject}
              <button
                type="button"
                class="btn-action btn-reject"
                on:click={handleReject}
                disabled={submitting}
              >
                ✗ Reject
              </button>
            {/if}
            {#if canRetry}
              <button
                type="button"
                class="btn-action btn-retry"
                on:click={handleRetry}
                disabled={submitting}
              >
                🔄 Retry
              </button>
            {/if}
            <button type="submit" class="btn-primary" disabled={submitting}>
              {submitting ? 'Saving...' : 'Save'}
            </button>
          </div>
        {:else}
          <div class="action-group-right">
            <button type="button" class="btn-secondary" on:click={() => dispatch('close')}>
              Cancel
            </button>
            <button type="submit" class="btn-primary" disabled={submitting}>
              {submitting ? 'Creating...' : 'Create Card'}
            </button>
          </div>
        {/if}
      </div>
    </form>
  </div>
</div>

<style>
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
    width: calc(100% - 2rem);
    max-width: min(1400px, 95vw);
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  }

  @media (max-width: 900px) {
    .modal {
      max-width: 100%;
      border-radius: 8px;
    }
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.25rem 1.5rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    color: var(--text-color, #cdd6f4);
  }

  .btn-close {
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    font-size: 1.25rem;
    cursor: pointer;
    padding: 0.25rem;
  }

  .btn-close:hover {
    color: var(--text-color, #cdd6f4);
  }

  form {
    padding: 1.5rem;
  }

  .form-group {
    margin-bottom: 1.25rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
  }

  .form-group input,
  .form-group textarea {
    width: 100%;
    padding: 0.75rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.95rem;
    font-family: inherit;
  }

  .form-group input::placeholder,
  .form-group textarea::placeholder {
    color: var(--text-muted, #6c7086);
  }

  .form-group input:focus,
  .form-group textarea:focus,
  .form-group select:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  .form-group select {
    width: 100%;
    padding: 0.75rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.95rem;
    font-family: inherit;
    cursor: pointer;
  }

  .form-hint {
    margin: 0.5rem 0 0;
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .card-meta {
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1.25rem;
  }

  .meta-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }

  .meta-item:last-child {
    margin-bottom: 0;
  }

  .meta-label {
    color: var(--text-muted, #6c7086);
    font-size: 0.85rem;
  }

  .meta-value {
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
  }

  .meta-value code {
    font-family: monospace;
    background: var(--badge-bg, #313244);
    padding: 0.15rem 0.4rem;
    border-radius: 4px;
  }

  .status-badge {
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-weight: 500;
  }

  .status-badge[data-status="todo"] { background: #89b4fa33; color: #89b4fa; }
  .status-badge[data-status="in_progress"] { background: #f9e2af33; color: #f9e2af; }
  .status-badge[data-status="in_review"] { background: #cba6f733; color: #cba6f7; }
  .status-badge[data-status="done"] { background: #a6e3a133; color: #a6e3a1; }
  .status-badge[data-status="failed"] { background: #f38ba833; color: #f38ba8; }

  .runner-type-badge {
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-weight: 500;
  }

  .runner-type-badge[data-runner="any"] { background: #6c708633; color: #6c7086; }
  .runner-type-badge[data-runner="claude-code"] { background: #f9a82533; color: #f9a825; }
  .runner-type-badge[data-runner="gemini"] { background: #4285f433; color: #4285f4; }

  .step-type-badge {
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-weight: 500;
  }

  .step-type-badge[data-step="agent"] { background: #89b4fa33; color: #89b4fa; }
  .step-type-badge[data-step="script"] { background: #a6e3a133; color: #a6e3a1; }
  .step-type-badge[data-step="docker"] { background: #cba6f733; color: #cba6f7; }

  .diff-section {
    margin-top: 1.25rem;
    padding-top: 1.25rem;
    border-top: 1px solid var(--border-color, #45475a);
  }

  .diff-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .diff-section h3 {
    margin: 0;
    font-size: 1rem;
    color: var(--text-color, #cdd6f4);
  }

  .diff-header-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .branch-selector {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .branch-selector label {
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
  }

  .branch-selector select {
    padding: 0.4rem 0.75rem;
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .branch-selector select:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  .btn-rebase {
    padding: 0.4rem 0.75rem;
    background: #89b4fa;
    color: #1e1e2e;
    border: none;
    border-radius: 4px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
  }

  .btn-rebase:hover:not(:disabled) {
    opacity: 0.9;
  }

  .btn-rebase:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .rebase-result {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 1rem;
    margin-top: 1rem;
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    border: 1px solid var(--border-color, #45475a);
  }

  .rebase-result.success {
    border-color: var(--success-color, #a6e3a1);
    background: rgba(166, 227, 161, 0.1);
  }

  .result-icon {
    font-size: 1.25rem;
    line-height: 1;
  }

  .rebase-result.success .result-icon {
    color: var(--success-color, #a6e3a1);
  }

  .result-info, .notice-info {
    flex: 1;
  }

  .result-message, .notice-message {
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
    margin-bottom: 0.25rem;
  }

  .result-sha, .notice-detail {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .result-sha code, .notice-detail {
    font-family: monospace;
    background: var(--badge-bg, #313244);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    color: var(--primary-color, #89b4fa);
  }

  .merge-result {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 1rem;
    margin-top: 1rem;
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    border: 1px solid var(--border-color, #45475a);
  }

  .merge-result.success {
    border-color: var(--success-color, #a6e3a1);
    background: rgba(166, 227, 161, 0.1);
  }

  .merge-icon {
    font-size: 1.25rem;
    line-height: 1;
  }

  .merge-result.success .merge-icon {
    color: var(--success-color, #a6e3a1);
  }

  .merge-info {
    flex: 1;
  }

  .merge-message {
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
    margin-bottom: 0.25rem;
  }

  .merge-type, .merge-sha {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .merge-sha code {
    font-family: monospace;
    background: var(--badge-bg, #313244);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    color: var(--primary-color, #89b4fa);
  }

  .modal-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color, #45475a);
  }

  .action-group-left {
    display: flex;
    gap: 0.5rem;
  }

  .action-group-right {
    display: flex;
    gap: 0.5rem;
    margin-left: auto;
  }

  .btn-primary,
  .btn-secondary,
  .btn-action,
  .btn-delete {
    padding: 0.6rem 1rem;
    border-radius: 6px;
    font-weight: 500;
    cursor: pointer;
    border: none;
    font-size: 0.9rem;
  }

  .btn-primary {
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
  }

  .btn-secondary {
    background: var(--surface-alt, #313244);
    color: var(--text-color, #cdd6f4);
  }

  .btn-delete {
    background: transparent;
    color: var(--error-color, #f38ba8);
    border: 1px solid var(--error-color, #f38ba8);
  }

  .btn-start {
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
  }

  .btn-approve {
    background: #a6e3a1;
    color: #1e1e2e;
  }

  .btn-reject {
    background: transparent;
    color: var(--error-color, #f38ba8);
    border: 1px solid var(--error-color, #f38ba8);
  }

  .btn-retry {
    background: #f9e2af;
    color: #1e1e2e;
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button:not(:disabled):hover {
    opacity: 0.9;
  }

  /* Step type selector styles */
  .step-type-selector {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .step-type-option {
    flex: 1;
    min-width: 150px;
    padding: 0.75rem;
    background: var(--surface-alt, #181825);
    border: 2px solid var(--border-color, #45475a);
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    transition: border-color 0.15s, background 0.15s;
  }

  .step-type-option:hover {
    border-color: var(--primary-color, #89b4fa);
    background: var(--input-bg, #181825);
  }

  .step-type-option.selected {
    border-color: var(--primary-color, #89b4fa);
    background: rgba(137, 180, 250, 0.1);
  }

  .step-type-label {
    display: block;
    font-weight: 600;
    color: var(--text-color, #cdd6f4);
    margin-bottom: 0.25rem;
  }

  .step-type-desc {
    display: block;
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .step-type-option.selected .step-type-label {
    color: var(--primary-color, #89b4fa);
  }

  /* Agent file selector styles */
  .agent-file-selector {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    padding: 0.75rem;
    max-height: 200px;
    overflow-y: auto;
  }

  .agent-checkbox {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    padding: 0.4rem;
    border-radius: 4px;
    transition: background 0.15s;
  }

  .agent-checkbox:hover {
    background: var(--badge-bg, #313244);
  }

  .agent-checkbox input[type="checkbox"] {
    width: auto;
    margin: 0;
  }

  .agent-name {
    font-family: monospace;
    font-weight: 500;
    color: var(--primary-color, #89b4fa);
    font-size: 0.9rem;
  }

  .agent-checkbox.repo-agent {
    cursor: default;
    opacity: 0.8;
  }

  .agent-source-badge {
    font-size: 0.65rem;
    padding: 0.1rem 0.4rem;
    background: var(--success-color, #a6e3a1);
    color: var(--surface-color, #1e1e2e);
    border-radius: 4px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .agent-desc {
    color: var(--text-muted, #6c7086);
    font-size: 0.8rem;
    margin-left: auto;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 200px;
  }

  .btn-toggle-advanced {
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    cursor: pointer;
    font-size: 0.85rem;
    padding: 0;
    text-decoration: underline;
  }

  .btn-toggle-advanced:hover {
    color: var(--text-color, #cdd6f4);
  }

  .form-hint code {
    background: var(--badge-bg, #313244);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    font-family: monospace;
    font-size: 0.85em;
  }

  /* Script/command textarea styling */
  .script-input {
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;
    font-size: 0.9rem;
    line-height: 1.4;
    resize: vertical;
    min-height: 60px;
  }
</style>
