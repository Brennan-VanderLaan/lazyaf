<script lang="ts">
  import { SvelteFlow, Background, Controls, MiniMap, type Node, type Edge, type Connection } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';

  import type {
    PipelineGraphModel,
    PipelineStepV2,
    PipelineEdge as PipelineEdgeType,
    EdgeCondition,
    RunStatus,
    StepType
  } from '../../api/types';

  import StepNode from './StepNode.svelte';
  import StartNode from './StartNode.svelte';
  import ConditionEdge from './ConditionEdge.svelte';
  import NodePalette from './NodePalette.svelte';
  import GraphToolbar from './GraphToolbar.svelte';
  import ContextMenu from './ContextMenu.svelte';
  import StepConfigModal from './StepConfigModal.svelte';

  // Special Start node ID
  const START_NODE_ID = '__start__';

  // Props
  interface Props {
    graph: PipelineGraphModel;
    stepStatuses?: Record<string, RunStatus>;  // For execution visualization
    activeStepIds?: string[];  // Currently executing steps
    completedStepIds?: string[];  // Completed steps
    readonly?: boolean;
    onGraphChange?: (graph: PipelineGraphModel) => void;
  }

  let {
    graph = $bindable(),
    stepStatuses = {},
    activeStepIds = [],
    completedStepIds = [],
    readonly = false,
    onGraphChange
  }: Props = $props();

  // Custom node and edge types
  const nodeTypes = {
    step: StepNode,
    start: StartNode,
  };

  const edgeTypes = {
    condition: ConditionEdge,
  };

  // Convert graph model to Svelte Flow format
  function graphToNodes(g: PipelineGraphModel): Node[] {
    // Start node - always present, positioned to the left
    const startNode: Node = {
      id: START_NODE_ID,
      type: 'start',
      position: g.start_position ?? { x: 50, y: 50 },
      data: { label: 'Start' },
      deletable: false,
      draggable: true,
    };

    // Step nodes
    const stepNodes = Object.values(g.steps).map(step => ({
      id: step.id,
      type: 'step',
      position: step.position ?? { x: 0, y: 0 },
      data: {
        step,
        status: stepStatuses[step.id],
        isEntryPoint: false, // Entry points now determined by Start node connections
        isActive: activeStepIds.includes(step.id),
        isCompleted: completedStepIds.includes(step.id),
        onEdit: () => openStepEditor(step.id),
        onDelete: () => deleteStep(step.id),
      },
    }));

    return [startNode, ...stepNodes];
  }

  function graphToEdges(g: PipelineGraphModel): Edge[] {
    // Convert existing edges
    const existingEdges = g.edges.map(edge => ({
      id: edge.id,
      source: edge.from_step,
      target: edge.to_step,
      type: 'condition',
      data: {
        condition: edge.condition,
        isActive: activeStepIds.includes(edge.from_step) || activeStepIds.includes(edge.to_step),
        isCompleted: completedStepIds.includes(edge.from_step),
        onConditionChange: (condition: EdgeCondition) => changeEdgeCondition(edge.id, condition),
      },
      animated: activeStepIds.includes(edge.from_step),
    }));

    // Check for entry_points that don't have edges from Start node
    // This handles loading old pipelines that have entry_points but no Start edges
    const startEdgeTargets = new Set(g.edges.filter(e => e.from_step === START_NODE_ID).map(e => e.to_step));
    const missingEntryEdges: Edge[] = [];

    for (const entryPoint of g.entry_points || []) {
      if (!startEdgeTargets.has(entryPoint) && g.steps[entryPoint]) {
        // Create a synthetic edge from Start to this entry point
        missingEntryEdges.push({
          id: `__start_to_${entryPoint}`,
          source: START_NODE_ID,
          target: entryPoint,
          type: 'condition',
          data: {
            condition: 'always' as EdgeCondition,
            isActive: activeStepIds.includes(entryPoint),
            isCompleted: false,
            onConditionChange: (condition: EdgeCondition) => {
              // When condition changes, persist as a real edge
              addStartEdge(entryPoint, condition);
            },
          },
          animated: false,
        });
      }
    }

    return [...existingEdges, ...missingEntryEdges];
  }

  // Add a real edge from Start node to a step (converts synthetic edge to real)
  function addStartEdge(targetStepId: string, condition: EdgeCondition) {
    const newEdge: PipelineEdgeType = {
      id: generateEdgeId(),
      from_step: START_NODE_ID,
      to_step: targetStepId,
      condition,
    };

    const newEdges = [...graph.edges.filter(e => !(e.from_step === START_NODE_ID && e.to_step === targetStepId)), newEdge];
    const newGraph: PipelineGraphModel = {
      ...graph,
      edges: newEdges,
      entry_points: deriveEntryPoints(newEdges),
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  // Reactive nodes and edges
  let nodes = $derived(graphToNodes(graph));
  let edges = $derived(graphToEdges(graph));

  // Track if we've migrated entry_points for this graph
  let migratedGraphId = $state<string | null>(null);

  // Migrate legacy entry_points to real Start edges on load
  $effect(() => {
    // Create a stable ID for this graph to avoid re-migration
    const graphId = Object.keys(graph.steps).sort().join(',');
    if (migratedGraphId === graphId) return;

    const startEdgeTargets = new Set(graph.edges.filter(e => e.from_step === START_NODE_ID).map(e => e.to_step));
    const missingEntryPoints = (graph.entry_points || []).filter(
      ep => !startEdgeTargets.has(ep) && graph.steps[ep]
    );

    if (missingEntryPoints.length > 0) {
      // Create real edges for legacy entry_points
      const newEdges = [...graph.edges];
      for (const entryPoint of missingEntryPoints) {
        newEdges.push({
          id: `edge_start_${entryPoint}`,
          from_step: START_NODE_ID,
          to_step: entryPoint,
          condition: 'always' as EdgeCondition,
        });
      }

      const newGraph: PipelineGraphModel = {
        ...graph,
        edges: newEdges,
        entry_points: deriveEntryPoints(newEdges),
      };

      graph = newGraph;
      onGraphChange?.(newGraph);
    }

    migratedGraphId = graphId;
  });

  // Context menu state
  let contextMenu = $state<{ x: number; y: number; flowPosition: { x: number; y: number } } | null>(null);

  // Step editor modal state
  let editingStep = $state<PipelineStepV2 | null>(null);
  let isNewStep = $state(false);

  // Generate unique step ID
  function generateStepId(): string {
    const existing = Object.keys(graph.steps);
    let i = existing.length + 1;
    while (existing.includes(`step_${i}`)) i++;
    return `step_${i}`;
  }

  // Generate unique edge ID
  function generateEdgeId(): string {
    const existing = graph.edges.map(e => e.id);
    let i = existing.length + 1;
    while (existing.includes(`edge_${i}`)) i++;
    return `edge_${i}`;
  }

  // Add a new step at position
  function addStep(type: StepType, position: { x: number; y: number }) {
    const id = generateStepId();
    const newStep: PipelineStepV2 = {
      id,
      name: `New ${type} step`,
      type,
      config: type === 'docker' ? { image: 'ubuntu:latest', command: 'echo hello' }
             : type === 'script' ? { command: 'echo hello' }
             : { title: 'AI Task', description: '' },
      position,
      timeout: 300,
    };

    // Open editor for the new step
    editingStep = newStep;
    isNewStep = true;
    contextMenu = null;
  }

  // Save step (new or edited)
  function saveStep(step: PipelineStepV2) {
    const newSteps = { ...graph.steps };
    newSteps[step.id] = step;

    // Entry points are now derived from Start node connections
    const newGraph: PipelineGraphModel = {
      ...graph,
      steps: newSteps,
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
    editingStep = null;
    isNewStep = false;
  }

  // Delete a step
  function deleteStep(stepId: string) {
    const newSteps = { ...graph.steps };
    delete newSteps[stepId];

    // Remove edges connected to this step
    const newEdges = graph.edges.filter(
      e => e.from_step !== stepId && e.to_step !== stepId
    );

    // Entry points derived from Start node connections
    const newGraph: PipelineGraphModel = {
      ...graph,
      steps: newSteps,
      edges: newEdges,
      entry_points: deriveEntryPoints(newEdges),
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  // Open step editor
  function openStepEditor(stepId: string) {
    editingStep = { ...graph.steps[stepId] };
    isNewStep = false;
  }

  // Handle node position changes
  function onNodesChange(changes: any[]) {
    if (readonly) return;

    let updated = false;
    const newSteps = { ...graph.steps };
    let newStartPosition = graph.start_position;

    for (const change of changes) {
      if (change.type === 'position' && change.position) {
        // Handle Start node position
        if (change.id === START_NODE_ID) {
          newStartPosition = change.position;
          updated = true;
        } else {
          // Handle step node positions
          const step = newSteps[change.id];
          if (step) {
            newSteps[change.id] = { ...step, position: change.position };
            updated = true;
          }
        }
      }
    }

    if (updated) {
      const newGraph: PipelineGraphModel = { ...graph, steps: newSteps, start_position: newStartPosition };
      graph = newGraph;
      onGraphChange?.(newGraph);
    }
  }

  // Helper: derive entry_points from edges connected to Start node
  function deriveEntryPoints(edges: PipelineEdgeType[]): string[] {
    return edges
      .filter(e => e.from_step === START_NODE_ID)
      .map(e => e.to_step);
  }

  // Handle new connections
  function onConnect(connection: Connection) {
    if (readonly || !connection.source || !connection.target) return;

    // Connections from Start node use 'always' condition
    const isFromStart = connection.source === START_NODE_ID;

    // Smart defaults: check if source already has a success edge
    const existingSuccessEdge = graph.edges.find(
      e => e.from_step === connection.source && e.condition === 'success'
    );

    // Default to 'always' for Start connections, otherwise success/failure logic
    const condition: EdgeCondition = isFromStart ? 'always' :
      (existingSuccessEdge ? 'failure' : 'success');

    const newEdge: PipelineEdgeType = {
      id: generateEdgeId(),
      from_step: connection.source,
      to_step: connection.target,
      condition,
    };

    const newEdges = [...graph.edges, newEdge];

    const newGraph: PipelineGraphModel = {
      ...graph,
      edges: newEdges,
      entry_points: deriveEntryPoints(newEdges),
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  // Handle edge deletion
  function onEdgesChange(changes: any[]) {
    if (readonly) return;

    const deletedIds = changes
      .filter(c => c.type === 'remove')
      .map(c => c.id);

    if (deletedIds.length > 0) {
      // Filter out deleted real edges
      const newEdges = graph.edges.filter(e => !deletedIds.includes(e.id));

      // Also handle synthetic edge deletion (e.g., __start_to_step_1)
      // These represent entry_points that don't have real edges yet
      const deletedSyntheticTargets = deletedIds
        .filter(id => id.startsWith('__start_to_'))
        .map(id => id.replace('__start_to_', ''));

      // Remove those from entry_points
      const newEntryPoints = (graph.entry_points || []).filter(
        ep => !deletedSyntheticTargets.includes(ep)
      );

      const newGraph: PipelineGraphModel = {
        ...graph,
        edges: newEdges,
        entry_points: newEntryPoints.length > 0 ? newEntryPoints : deriveEntryPoints(newEdges),
      };

      graph = newGraph;
      onGraphChange?.(newGraph);
    }
  }

  // Handle right-click on canvas
  function onPaneContextMenu(event: MouseEvent) {
    if (readonly) return;
    event.preventDefault();

    // Get flow position from mouse event
    // This will be handled by the SvelteFlow instance
    contextMenu = {
      x: event.clientX,
      y: event.clientY,
      flowPosition: { x: event.clientX - 250, y: event.clientY - 100 }, // Approximate
    };
  }

  // Handle edge condition change
  function changeEdgeCondition(edgeId: string, condition: EdgeCondition) {
    const newEdges = graph.edges.map(e =>
      e.id === edgeId ? { ...e, condition } : e
    );

    const newGraph: PipelineGraphModel = {
      ...graph,
      edges: newEdges,
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  // Add step from toolbar
  function onToolbarAddStep(type: StepType) {
    // Calculate position based on existing nodes to avoid overlap
    const existingCount = Object.keys(graph.steps).length;
    // Stagger horizontally for each new node
    const x = 200 + (existingCount * 250);
    const y = 200;
    addStep(type, { x, y });
  }

  // Add step from palette (drag)
  function onPaletteDropStep(type: StepType, position: { x: number; y: number }) {
    addStep(type, position);
  }
</script>

<div class="graph-editor" class:readonly>
  <!-- Toolbar -->
  {#if !readonly}
    <GraphToolbar onAddStep={onToolbarAddStep} />
  {/if}

  <div class="graph-container">
    <!-- Node Palette (Sidebar) -->
    {#if !readonly}
      <NodePalette onDropStep={onPaletteDropStep} />
    {/if}

    <!-- Main Flow Canvas -->
    <div
      class="flow-wrapper"
      oncontextmenu={(e) => { if (!readonly) onPaneContextMenu(e); }}
      ondragover={(e) => { e.preventDefault(); e.dataTransfer!.dropEffect = 'copy'; }}
      ondrop={(e) => {
        e.preventDefault();
        const type = e.dataTransfer?.getData('application/pipeline-node') as StepType;
        if (type && !readonly) {
          const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
          addStep(type, { x: e.clientX - rect.left, y: e.clientY - rect.top });
        }
      }}
    >
      <SvelteFlow
        {nodes}
        {edges}
        {nodeTypes}
        {edgeTypes}
        fitView
        snapToGrid
        snapGrid={[20, 20]}
        deleteKeyCode={readonly ? null : 'Backspace'}
        onnodeschange={onNodesChange}
        onedgeschange={onEdgesChange}
        onconnect={onConnect}
        onpanecontextmenu={onPaneContextMenu}
      >
        <Background variant="dots" gap={20} size={1} />
        <Controls />
        <MiniMap
          nodeColor={(node) => {
            const status = node.data?.status;
            if (status === 'running') return 'var(--warning-color)';
            if (status === 'passed') return 'var(--success-color)';
            if (status === 'failed') return 'var(--error-color)';
            return 'var(--surface-color)';
          }}
        />
      </SvelteFlow>
    </div>
  </div>

  <!-- Context Menu -->
  {#if contextMenu}
    <ContextMenu
      x={contextMenu.x}
      y={contextMenu.y}
      onAddStep={(type) => addStep(type, contextMenu!.flowPosition)}
      onClose={() => contextMenu = null}
    />
  {/if}

  <!-- Step Config Modal -->
  {#if editingStep}
    <StepConfigModal
      step={editingStep}
      isNew={isNewStep}
      onSave={saveStep}
      onCancel={() => { editingStep = null; isNewStep = false; }}
    />
  {/if}
</div>

<style>
  .graph-editor {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-color);
    border-radius: 8px;
    overflow: hidden;
  }

  .graph-editor.readonly {
    pointer-events: auto;
  }

  .graph-container {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  .flow-wrapper {
    flex: 1;
    position: relative;
  }

  /* Override Svelte Flow styles to match theme */
  :global(.svelte-flow) {
    background: var(--bg-color) !important;
  }

  :global(.svelte-flow__background) {
    background: var(--bg-color) !important;
  }

  :global(.svelte-flow__background pattern circle) {
    fill: var(--border-color) !important;
  }

  :global(.svelte-flow__controls) {
    background: var(--surface-color) !important;
    border: 1px solid var(--border-color) !important;
    border-radius: 6px !important;
  }

  :global(.svelte-flow__controls-button) {
    background: var(--surface-color) !important;
    border-color: var(--border-color) !important;
    fill: var(--text-color) !important;
  }

  :global(.svelte-flow__controls-button:hover) {
    background: var(--hover-color) !important;
  }

  :global(.svelte-flow__minimap) {
    background: var(--surface-color) !important;
    border: 1px solid var(--border-color) !important;
    border-radius: 6px !important;
  }

  :global(.svelte-flow__minimap-mask) {
    fill: var(--bg-color) !important;
    opacity: 0.8 !important;
  }

  :global(.svelte-flow__edge-path) {
    stroke-width: 2 !important;
  }

  :global(.svelte-flow__handle) {
    width: 14px !important;
    height: 14px !important;
    background: var(--primary-color) !important;
    border: 2px solid var(--surface-color) !important;
    z-index: 10 !important;
    pointer-events: auto !important;
  }

  :global(.svelte-flow__handle:hover) {
    background: var(--text-color) !important;
    transform: scale(1.3);
  }
</style>
