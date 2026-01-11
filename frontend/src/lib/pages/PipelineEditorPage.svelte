<script lang="ts">
  import { onMount } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { PipelineGraphEditor } from '../components/graph';
  import type { PipelineV2, PipelineGraphModel } from '../api/types';

  interface Props {
    params: { id?: string };
  }

  let { params }: Props = $props();

  // State
  let pipeline = $state<PipelineV2 | null>(null);
  let pipelineName = $state('');
  let pipelineDescription = $state('');
  let loading = $state(true);
  let saving = $state(false);
  let error = $state<string | null>(null);
  let hasUnsavedChanges = $state(false);

  // Graph state
  let graph = $state<PipelineGraphModel>({
    steps: {},
    edges: [],
    entry_points: [],
    version: 2,
  });

  // Is this a new pipeline?
  let isNew = $derived(params.id === 'new');

  // Load pipeline on mount
  onMount(async () => {
    if (isNew) {
      // New pipeline - start with empty graph
      loading = false;
      return;
    }

    if (!params.id) {
      error = 'Pipeline ID not provided';
      loading = false;
      return;
    }

    try {
      // Load the pipeline
      const response = await fetch(`/api/pipelines/${params.id}`);
      if (!response.ok) {
        throw new Error('Failed to load pipeline');
      }

      const data: PipelineV2 = await response.json();
      pipeline = data;
      pipelineName = data.name;
      pipelineDescription = data.description || '';

      // Load graph or convert from legacy
      if (data.steps_graph) {
        graph = data.steps_graph;
      } else if (data.steps && data.steps.length > 0) {
        // Convert legacy steps to graph
        graph = convertLegacyToGraph(data.steps);
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load pipeline';
    } finally {
      loading = false;
    }
  });

  // Convert legacy array-based steps to graph model
  function convertLegacyToGraph(steps: any[]): PipelineGraphModel {
    const graphSteps: Record<string, any> = {};
    const edges: any[] = [];

    steps.forEach((step, i) => {
      const stepId = `step_${i}`;
      graphSteps[stepId] = {
        id: stepId,
        name: step.name,
        type: step.type,
        config: step.config || {},
        position: { x: 100, y: i * 150 },
        timeout: step.timeout || 300,
        continue_in_context: step.continue_in_context || false,
      };

      // Create edges based on on_success/on_failure
      if (i < steps.length - 1 && step.on_success === 'next') {
        edges.push({
          id: `edge_${i}_success`,
          from_step: stepId,
          to_step: `step_${i + 1}`,
          condition: 'success',
        });
      }
    });

    return {
      steps: graphSteps,
      edges,
      entry_points: steps.length > 0 ? ['step_0'] : [],
      version: 2,
    };
  }

  // Handle graph changes
  function onGraphChange(newGraph: PipelineGraphModel) {
    graph = newGraph;
    hasUnsavedChanges = true;
  }

  // Save pipeline
  async function savePipeline() {
    if (!pipelineName.trim()) {
      error = 'Pipeline name is required';
      return;
    }

    if (Object.keys(graph.steps).length === 0) {
      error = 'Pipeline must have at least one step';
      return;
    }

    if (graph.entry_points.length === 0) {
      error = 'Pipeline must have at least one entry point';
      return;
    }

    saving = true;
    error = null;

    try {
      const payload = {
        name: pipelineName,
        description: pipelineDescription || null,
        steps_graph: graph,
        steps: [], // Empty legacy steps since we're using graph
      };

      let response: Response;

      if (isNew) {
        // Get repo_id from URL query (supports hash-based routing)
        // With hash routing, URL is like /#/pipelines/new/edit?repo_id=xxx
        const hash = window.location.hash;
        const queryIndex = hash.indexOf('?');
        const queryString = queryIndex >= 0 ? hash.substring(queryIndex + 1) : '';
        const urlParams = new URLSearchParams(queryString);
        const repoId = urlParams.get('repo_id');

        if (!repoId) {
          error = 'Repository ID is required';
          saving = false;
          return;
        }

        response = await fetch(`/api/repos/${repoId}/pipelines`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } else {
        response = await fetch(`/api/pipelines/${params.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      }

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to save pipeline');
      }

      hasUnsavedChanges = false;

      // Navigate back to pipelines list
      push('/pipelines');
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save pipeline';
    } finally {
      saving = false;
    }
  }

  // Cancel and go back
  function cancel() {
    if (hasUnsavedChanges) {
      if (!confirm('You have unsaved changes. Are you sure you want to leave?')) {
        return;
      }
    }
    push('/pipelines');
  }

  // Warn before leaving with unsaved changes
  function handleBeforeUnload(event: BeforeUnloadEvent) {
    if (hasUnsavedChanges) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
</script>

<svelte:window on:beforeunload={handleBeforeUnload} />

<div class="editor-page">
  <!-- Header -->
  <header class="editor-header">
    <div class="header-left">
      <button class="back-btn" onclick={cancel} title="Back to Pipelines">
        <span class="back-arrow">←</span>
        <span>Pipelines</span>
      </button>

      <div class="pipeline-info">
        <input
          type="text"
          class="pipeline-name"
          bind:value={pipelineName}
          placeholder="Pipeline name..."
          oninput={() => hasUnsavedChanges = true}
        />
        <input
          type="text"
          class="pipeline-desc"
          bind:value={pipelineDescription}
          placeholder="Description (optional)"
          oninput={() => hasUnsavedChanges = true}
        />
      </div>
    </div>

    <div class="header-right">
      {#if hasUnsavedChanges}
        <span class="unsaved-indicator">Unsaved changes</span>
      {/if}

      <button class="btn secondary" onclick={cancel} disabled={saving}>
        Cancel
      </button>
      <button class="btn primary" onclick={savePipeline} disabled={saving || loading}>
        {#if saving}
          Saving...
        {:else}
          Save Pipeline
        {/if}
      </button>
    </div>
  </header>

  <!-- Error message -->
  {#if error}
    <div class="error-banner">
      <span>{error}</span>
      <button onclick={() => error = null}>&times;</button>
    </div>
  {/if}

  <!-- Main content -->
  <main class="editor-content">
    {#if loading}
      <div class="loading">
        <span>Loading pipeline...</span>
      </div>
    {:else}
      <PipelineGraphEditor
        bind:graph
        onGraphChange={onGraphChange}
      />
    {/if}
  </main>
</div>

<style>
  .editor-page {
    display: flex;
    flex-direction: column;
    height: 100vh;
    background: var(--bg-color);
  }

  .editor-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 20px;
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
    gap: 20px;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 20px;
    flex: 1;
  }

  .back-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 8px 12px;
    background: transparent;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-muted);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .back-btn:hover {
    background: var(--hover-color);
    color: var(--text-color);
    border-color: var(--text-muted);
  }

  .back-arrow {
    font-size: 16px;
  }

  .pipeline-info {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
    max-width: 400px;
  }

  .pipeline-name {
    font-size: 16px;
    font-weight: 600;
    color: var(--text-color);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 4px 0;
    outline: none;
    transition: border-color 0.15s ease;
  }

  .pipeline-name:focus {
    border-bottom-color: var(--primary-color);
  }

  .pipeline-name::placeholder {
    color: var(--text-muted);
  }

  .pipeline-desc {
    font-size: 13px;
    color: var(--text-muted);
    background: transparent;
    border: none;
    padding: 2px 0;
    outline: none;
  }

  .pipeline-desc:focus {
    color: var(--text-color);
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .unsaved-indicator {
    font-size: 12px;
    color: var(--warning-color);
    padding: 4px 10px;
    background: rgba(249, 226, 175, 0.1);
    border-radius: 4px;
  }

  .btn {
    padding: 10px 20px;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn.secondary {
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .btn.secondary:hover:not(:disabled) {
    background: var(--hover-color);
  }

  .btn.primary {
    background: var(--primary-color);
    color: var(--primary-text);
  }

  .btn.primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .error-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 20px;
    background: rgba(243, 139, 168, 0.1);
    border-bottom: 1px solid var(--error-color);
    color: var(--error-color);
    font-size: 13px;
  }

  .error-banner button {
    background: transparent;
    border: none;
    color: var(--error-color);
    font-size: 18px;
    cursor: pointer;
    padding: 0 5px;
  }

  .editor-content {
    flex: 1;
    overflow: hidden;
  }

  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-muted);
    font-size: 14px;
  }
</style>
