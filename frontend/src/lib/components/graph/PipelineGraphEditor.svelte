<script lang="ts">
  import { setContext } from 'svelte';
  import { SvelteFlow, Background, BackgroundVariant, Controls, MiniMap, type Node, type Edge, type Connection } from '@xyflow/svelte';
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
  import ConnectPanel from './ConnectPanel.svelte';
  import StepConfigModal from './StepConfigModal.svelte';
  import { GRAPH_ACTIONS, type GraphActions } from './actions';
  import { graphStepList } from './order';

  // Special Start node ID. It is an AUTHORING device only: it never appears
  // in `graph.steps` and never in `graph.edges` - see graphToEdges.
  const START_NODE_ID = '__start__';

  // Id prefix for the display-only edges drawn from Start to each entry point.
  const START_EDGE_PREFIX = '__start_to_';

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

  // Where the Start node sits when the graph has never recorded a position for
  // it. It has to clear the FIRST STEP, and the v1 -> v2 migration parks step 0
  // at { x: 100, y: 0 }. The old default of { x: 50, y: 50 } put this 64x64
  // circle at 50..114 x 50..114, which overlapped that step's 100..280 x 0..61
  // box by 14x21px on every legacy pipeline - the START disc sat on the first
  // step's lower-left corner.
  //
  // { x: 0, y: 0 } leaves a 36px gap to step 0 and shares its row. It is
  // deliberately NOT negative: `fitView` does not reliably reframe this canvas
  // (measured - on a client-side route into the editor the viewport transform
  // stays translate(0,0) scale(1)), and under an identity transform a node at a
  // negative x is simply off the left edge and invisible.
  const DEFAULT_START_POSITION = { x: 0, y: 0 };

  // Convert graph model to Svelte Flow format
  function graphToNodes(g: PipelineGraphModel): Node[] {
    // Start node - always present, positioned to the left
    const startNode: Node = {
      id: START_NODE_ID,
      type: 'start',
      position: g.start_position ?? DEFAULT_START_POSITION,
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
        // Was hardcoded `false` under a comment saying entry points are
        // "determined by Start node connections" - which made the flag a
        // lie for every entry point. They are determined by `entry_points`.
        isEntryPoint: (g.entry_points ?? []).includes(step.id),
        isActive: activeStepIds.includes(step.id),
        isCompleted: completedStepIds.includes(step.id),
        onEdit: () => openStepEditor(step.id),
        onDelete: () => deleteStep(step.id),
      },
    }));

    return [startNode, ...stepNodes];
  }

  /**
   * ENTRY POINTS ARE NOT EDGES.
   *
   * `PipelineGraphModel.entry_points` is the ONE representation of "the run
   * starts here"; `edges` may only reference real step ids, and the backend
   * enforces exactly that (`validate_graph_integrity`: "Edge 'X' references
   * non-existent from_step"). The Start node is an authoring device - a
   * visible thing to drag a connection out of - so its edges are SYNTHESIZED
   * here for display and never enter the model.
   *
   * This is a bug fix, and it is the reason nothing could be saved: the
   * editor used to push real `{from_step: '__start__'}` edges into
   * `graph.edges`, so the moment an author declared an entry point the way
   * the UI tells them to, POST/PATCH came back 422. The only spec that would
   * have caught it was one of the nine skipped ones - and it was skipped
   * because it could not reach a save without connecting Start first.
   *
   * NOTE ON `data`: it holds PLAIN VALUES ONLY - no callbacks. Svelte Flow
   * decides whether to warn "Use $state.raw for edges" by attempting
   * `structuredClone(edges[0])` (node_modules/@xyflow/svelte/dist/lib/store/
   * initial-store.svelte.js), and a function in `data` makes that throw,
   * which it then misreports as deep reactivity. The edge's own handlers
   * reach the editor through the GRAPH_ACTIONS context instead, keyed by
   * edge id - which is also what the library documents `data` should be:
   * serialisable.
   */
  function graphToEdges(g: PipelineGraphModel): Edge[] {
    // Step-to-step edges. A `__start__` edge from a graph saved by the older
    // editor is dropped rather than rendered: it is exactly the shape the
    // API refuses, and the entry point it stood for is in `entry_points`.
    const existingEdges = g.edges.filter(e => e.from_step !== START_NODE_ID).map(edge => ({
      id: edge.id,
      source: edge.from_step,
      target: edge.to_step,
      type: 'condition',
      data: {
        condition: edge.condition,
        isActive: activeStepIds.includes(edge.from_step) || activeStepIds.includes(edge.to_step),
        isCompleted: completedStepIds.includes(edge.from_step),
      },
      animated: activeStepIds.includes(edge.from_step),
    }));

    // One synthetic edge per entry point, drawn from Start. `__start_to_` is
    // the marker that says "this line is an entry point, not an edge"; every
    // handler below tests for it.
    const entryEdges: Edge[] = [];
    for (const entryPoint of g.entry_points || []) {
      if (!g.steps[entryPoint]) continue;
      entryEdges.push({
        id: `${START_EDGE_PREFIX}${entryPoint}`,
        source: START_NODE_ID,
        target: entryPoint,
        type: 'condition',
        data: {
          condition: 'always' as EdgeCondition,
          isActive: activeStepIds.includes(entryPoint),
          isCompleted: false,
          isEntry: true,
        },
        animated: false,
      });
    }

    return [...existingEdges, ...entryEdges];
  }

  // Reactive nodes and edges
  let nodes = $derived(graphToNodes(graph));
  let edges = $derived(graphToEdges(graph));

  // Context menu state
  let contextMenu = $state<{ x: number; y: number; flowPosition: { x: number; y: number } } | null>(null);

  // Connect panel state (the keyboard path to an edge - see ConnectPanel).
  let connecting = $state(false);

  /**
   * Dismiss whatever overlay is open, from a listener that is attached for
   * the WHOLE life of the editor.
   *
   * This is the fix for a real defect, and the reason it lives HERE rather
   * than in ContextMenu: the menu used to register its own `document`
   * keydown listener inside `onMount` -> `setTimeout(..., 0)` (the delay is
   * there so the click that OPENS it does not immediately close it). The
   * timeout is a macrotask, and opening the menu queues a SvelteFlow
   * re-render ahead of it, so between the menu becoming visible and its
   * listener existing there is a window in which Escape does nothing. It is
   * not theoretical: pressing Escape immediately left the menu open every
   * time, while pressing it after a pause closed it - which is exactly the
   * shape of a bug that gets "fixed" and then reappears, because whether it
   * reproduces depends on how fast you press the key.
   *
   * A `svelte:window` handler is bound when the EDITOR mounts, long before
   * any overlay can open, so there is no window to lose.
   */
  function onWindowKeydown(event: KeyboardEvent) {
    if (event.key !== 'Escape') return;
    if (contextMenu) contextMenu = null;
    if (connecting) connecting = false;
  }

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

    // ...and stop calling it an entry point. A deleted step left in
    // `entry_points` is what `validate_graph_integrity` refuses as "Entry
    // point 'X' references non-existent step".
    const newGraph: PipelineGraphModel = {
      ...graph,
      steps: newSteps,
      edges: newEdges,
      entry_points: (graph.entry_points ?? []).filter(ep => ep !== stepId),
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  // Open step editor
  function openStepEditor(stepId: string) {
    editingStep = { ...graph.steps[stepId] };
    isNewStep = false;
  }

  /**
   * Persist a node's new position into the graph after a drag.
   *
   * THIS USED TO BE `onnodeschange`, WHICH @xyflow/svelte v1 DOES NOT HAVE.
   * The prop was accepted as an unknown attribute and never called, so
   * dragging a node moved it on screen and changed nothing in `graph` - the
   * layout was lost on the next save and on every re-render from a WebSocket
   * frame. `onnodedragstop` is the v1 event and carries the moved nodes with
   * their final positions, which is exactly (and only) what this needs.
   */
  function onNodeDragStop({ nodes: dragged }: { nodes: Node[] }) {
    if (readonly) return;

    let updated = false;
    const newSteps = { ...graph.steps };
    let newStartPosition = graph.start_position;

    for (const node of dragged) {
      if (!node.position) continue;
      if (node.id === START_NODE_ID) {
        newStartPosition = { ...node.position };
        updated = true;
      } else {
        const step = newSteps[node.id];
        if (step) {
          newSteps[node.id] = { ...step, position: { ...node.position } };
          updated = true;
        }
      }
    }

    if (updated) {
      const newGraph: PipelineGraphModel = { ...graph, steps: newSteps, start_position: newStartPosition };
      graph = newGraph;
      onGraphChange?.(newGraph);
    }
  }

  /** Declare `stepId` an entry point (idempotent). */
  function addEntryPoint(stepId: string) {
    if ((graph.entry_points ?? []).includes(stepId)) return;
    const newGraph: PipelineGraphModel = {
      ...graph,
      entry_points: [...(graph.entry_points ?? []), stepId],
    };
    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  // (Un-declaring an entry point is `removeEdgeIds`, which is where the
  // `__start_to_` line the author deletes actually arrives.)

  /**
   * The condition a NEW edge out of `sourceId` gets when the author has not
   * said otherwise.
   *
   * ONE definition, called by both authoring paths - the handle drag and the
   * Connect panel. It used to be inline in `onConnect`, which meant the panel
   * would have had to restate it and the two could then disagree about what
   * "the default" is while both looked right in isolation (R3).
   */
  function defaultConditionFor(sourceId: string): EdgeCondition {
    // Start feeds an ENTRY POINT; there is no outcome to branch on yet.
    if (sourceId === START_NODE_ID) return 'always';
    // Smart default: the first edge out of a step is its success path, the
    // second is its failure path - the shape a v1 `on_success`/`on_failure`
    // pair used to spell.
    const hasSuccessEdge = graph.edges.some(
      e => e.from_step === sourceId && e.condition === 'success'
    );
    return hasSuccessEdge ? 'failure' : 'success';
  }

  /**
   * Draw one connection. Both authoring paths (handle drag, Connect panel)
   * land here.
   *
   * A connection FROM START is not an edge - it declares an entry point. See
   * graphToEdges for why that distinction is load-bearing rather than
   * cosmetic.
   */
  function createEdge(fromStep: string, toStep: string, condition: EdgeCondition) {
    if (readonly || !fromStep || !toStep) return;

    if (fromStep === START_NODE_ID) {
      addEntryPoint(toStep);
      return;
    }

    const newEdge: PipelineEdgeType = {
      id: generateEdgeId(),
      from_step: fromStep,
      to_step: toStep,
      condition,
    };

    const newGraph: PipelineGraphModel = {
      ...graph,
      edges: [...graph.edges, newEdge],
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  // Handle new connections (handle drag)
  function onConnect(connection: Connection) {
    if (readonly || !connection.source || !connection.target) return;
    createEdge(connection.source, connection.target, defaultConditionFor(connection.source));
  }

  /**
   * Apply a library-initiated delete (the `deleteKey`, Backspace) to the graph.
   *
   * THIS USED TO BE `onedgeschange`, WHICH v1 DOES NOT HAVE - same dead-prop
   * story as `onnodeschange` above, and paired with `deleteKeyCode` (v1
   * spells it `deleteKey`), so neither half of Backspace-to-delete was
   * connected to anything. `ondelete` is the v1 event and reports BOTH the
   * deleted nodes and the deleted edges, so a deleted node takes its edges
   * with it the same way the Delete button on the node already did.
   */
  function onDelete({ nodes: removedNodes, edges: removedEdges }: { nodes: Node[]; edges: Edge[] }) {
    if (readonly) return;
    for (const node of removedNodes) {
      if (node.id !== START_NODE_ID) deleteStep(node.id);
    }
    if (removedEdges.length > 0) {
      removeEdgeIds(removedEdges.map(e => e.id));
    }
  }

  /**
   * Remove edges by id. A `__start_to_<step>` id is not an edge: deleting
   * that line means "this step is no longer an entry point".
   */
  function removeEdgeIds(deletedIds: string[]) {
    if (readonly || deletedIds.length === 0) return;

    const droppedEntryPoints = deletedIds
      .filter(id => id.startsWith(START_EDGE_PREFIX))
      .map(id => id.slice(START_EDGE_PREFIX.length));

    const newGraph: PipelineGraphModel = {
      ...graph,
      edges: graph.edges.filter(e => !deletedIds.includes(e.id)),
      entry_points: (graph.entry_points ?? []).filter(ep => !droppedEntryPoints.includes(ep)),
    };

    graph = newGraph;
    onGraphChange?.(newGraph);
  }

  /**
   * SvelteFlow's pane callback hands over `{ event }`, NOT the event.
   *
   * `onpanecontextmenu={onPaneContextMenu}` therefore called
   * `.preventDefault()` on a plain object and threw
   * `event.preventDefault is not a function` into the console on EVERY
   * right-click. The menu still appeared only because the native event went
   * on to bubble to `.flow-wrapper`'s own `oncontextmenu` below - i.e. the
   * feature looked fine while one of its two handlers was dead. This is also
   * one of the two `svelte-check` errors this file carried.
   */
  function onPaneContextMenuFromFlow({ event }: { event: MouseEvent }) {
    onPaneContextMenu(event);
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

  // Delete an edge by id (the picker's "Delete Edge"). Start lines are
  // entry points, so `removeEdgeIds` un-declares them rather than deleting.
  function deleteEdge(edgeId: string) {
    removeEdgeIds([edgeId]);
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

  // Add step from the palette by KEYBOARD. The palette items advertise
  // role="button" and take focus, so Enter/Space have to do something; drag and
  // drop cannot be performed from a keyboard. Same landing spot the toolbar
  // uses, so the new node cannot appear on top of an existing one.
  function onPaletteAddStep(type: StepType) {
    onToolbarAddStep(type);
  }

  // Edge handlers reached by ConditionEdge through context rather than through
  // `edge.data` - see the note on graphToEdges.
  const graphActions: GraphActions = {
    setEdgeCondition(edgeId: string, condition: EdgeCondition) {
      // A `__start_to_<step>` line is an ENTRY POINT, and an entry point has
      // no condition to set - the run starts there unconditionally. Doing
      // nothing quietly would be dark, so the picker does not offer the three
      // conditions on a Start line at all (ConditionEdge reads `isEntry`);
      // this arm exists so that a stale id can never write a `__start__`
      // edge into the model, which is what used to happen here and is
      // exactly what the API refuses.
      if (edgeId.startsWith(START_EDGE_PREFIX)) return;
      changeEdgeCondition(edgeId, condition);
    },
    deleteEdge(edgeId: string) {
      deleteEdge(edgeId);
    },
  };
  setContext(GRAPH_ACTIONS, graphActions);
</script>

<svelte:window on:keydown={onWindowKeydown} />

<div class="graph-editor" data-testid="graph-editor" class:readonly>
  <!-- Toolbar -->
  {#if !readonly}
    <GraphToolbar onAddStep={onToolbarAddStep} onConnect={() => connecting = true} />
  {/if}

  <div class="graph-container">
    <!-- Node Palette (Sidebar) -->
    {#if !readonly}
      <NodePalette onDropStep={onPaletteDropStep} onAddStep={onPaletteAddStep} />
    {/if}

    <!-- Main Flow Canvas.
         TWO PROPS BELOW WERE MISSPELLED FOR @xyflow/svelte v1 and were
         therefore doing NOTHING: `snapToGrid` (v1 turns snapping on from
         `snapGrid` alone) and `deleteKeyCode` (v1 spells it `deleteKey`).
         The second one matters: Backspace-to-delete a selected node or edge
         was simply not wired up, and in `readonly` mode the `null` that was
         meant to disable it was not being applied either. Both showed as
         svelte-check errors that nothing gated. -->
    <div
      class="flow-wrapper"
      data-testid="graph-canvas"
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
        snapGrid={[20, 20]}
        deleteKey={readonly ? null : 'Backspace'}
        onnodedragstop={onNodeDragStop}
        ondelete={onDelete}
        onconnect={onConnect}
        onpanecontextmenu={onPaneContextMenuFromFlow}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
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

      <!-- Connect panel: the keyboard/menu path to an edge. -->
      {#if connecting && !readonly}
        <ConnectPanel
          steps={graphStepList(graph)}
          edges={graph.edges}
          entryPoints={graph.entry_points ?? []}
          startNodeId={START_NODE_ID}
          {defaultConditionFor}
          onConnect={createEdge}
          onClose={() => connecting = false}
        />
      {/if}
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
