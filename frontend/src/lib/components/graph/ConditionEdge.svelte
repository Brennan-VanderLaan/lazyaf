<script lang="ts">
  import { getContext } from 'svelte';
  import { BaseEdge, EdgeLabel, getBezierPath, type EdgeProps } from '@xyflow/svelte';
  import type { EdgeCondition } from '../../api/types';
  import { GRAPH_ACTIONS, type GraphActions } from './actions';

  // `data` carries PLAIN VALUES ONLY. The edit handlers come from the editor
  // through context, keyed by this edge's own id — a function inside `data`
  // makes Svelte Flow's structuredClone probe throw and log a spurious
  // "Use $state.raw for edges" warning. See ./actions.ts.
  interface Props extends EdgeProps {
    data?: {
      condition: EdgeCondition;
      isActive?: boolean;
      isCompleted?: boolean;
      /**
       * True for the display-only line from Start to an entry point. An
       * entry point is where the run BEGINS, so there is no outcome for it
       * to be conditional on - offering "On success / On failure / Always"
       * there would be offering a choice the model cannot hold, and the one
       * that used to be accepted wrote a `__start__` edge that the API
       * refused with a 422 on save.
       */
      isEntry?: boolean;
    };
  }

  const actions = getContext<GraphActions | undefined>(GRAPH_ACTIONS);

  let {
    id,
    source,
    target,
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    data,
    selected,
    markerEnd,
  }: Props = $props();

  // A self-loop is an edge whose two ends are the SAME node. This used to be
  // guessed from screen distance — `Math.abs(sourceX - targetX) < 50 &&
  // Math.abs(sourceY - targetY) < 50` — and that proxy was wrong in BOTH
  // directions, which is why it is now decided from the node ids the edge
  // already carries:
  //
  //   - two DISTINCT nodes sitting close together got a circular loop drawn
  //     between them instead of a connector. Measured: once the Start node was
  //     moved clear of step_0, its source handle at x=64 sat 36px from that
  //     step's target handle, under the threshold, and the Start -> first step
  //     edge of every legacy pipeline rendered as a teardrop hanging in space;
  //   - a REAL self-loop was never detected at all. A step node is 180px wide,
  //     so its own source and target handles are ~180px apart — always over the
  //     threshold — and a step wired back to itself drew a degenerate bezier
  //     rather than a loop.
  let isSelfLoop = $derived(source === target);

  // Generate self-loop path (circular loop above the node)
  function getSelfLoopPath(x: number, y: number): [string, number, number] {
    const loopSize = 60;
    const path = `M ${x} ${y - 10}
                  C ${x - loopSize} ${y - loopSize},
                    ${x + loopSize} ${y - loopSize},
                    ${x} ${y - 10}`;
    return [path, x, y - loopSize - 10]; // labelX, labelY above the loop
  }

  // Get path and label position
  let [edgePath, labelX, labelY] = $derived(
    isSelfLoop
      ? getSelfLoopPath(sourceX, sourceY)
      : getBezierPath({
          sourceX,
          sourceY,
          sourcePosition,
          targetX,
          targetY,
          targetPosition,
        })
  );

  // Condition colors
  const conditionStyles: Record<EdgeCondition, { color: string; label: string; icon: string }> = {
    success: { color: 'var(--success-color)', label: 'success', icon: 'checkmark' },
    failure: { color: 'var(--error-color)', label: 'failure', icon: 'x' },
    always: { color: 'var(--primary-color)', label: 'always', icon: 'arrow' },
  };

  let condition = $derived(data?.condition ?? 'success');
  let style = $derived(conditionStyles[condition]);
  let isActive = $derived(data?.isActive ?? false);
  let isCompleted = $derived(data?.isCompleted ?? false);
  let isEntry = $derived(data?.isEntry ?? false);

  // Condition selector dropdown
  let showConditionPicker = $state(false);

  function setCondition(newCondition: EdgeCondition) {
    actions?.setEdgeCondition(id, newCondition);
    showConditionPicker = false;
  }
</script>

<g class="condition-edge" class:active={isActive} class:completed={isCompleted}>
  <!-- Main edge path -->
  <BaseEdge
    {id}
    path={edgePath}
    style="stroke: {style.color}; stroke-width: {selected ? 3 : 2};"
    {markerEnd}
  />

  <!-- Animated particles for active edges -->
  {#if isActive}
    <circle r="4" fill={style.color} class="flow-particle">
      <animateMotion dur="1s" repeatCount="indefinite" path={edgePath} />
    </circle>
    <circle r="4" fill={style.color} class="flow-particle" style="animation-delay: -0.5s">
      <animateMotion dur="1s" repeatCount="indefinite" path={edgePath} begin="0.33s" />
    </circle>
    <circle r="4" fill={style.color} class="flow-particle" style="animation-delay: -0.25s">
      <animateMotion dur="1s" repeatCount="indefinite" path={edgePath} begin="0.66s" />
    </circle>
  {/if}
</g>

<!-- Edge Label (condition badge) -->
<EdgeLabel
  x={labelX}
  y={labelY}
  class="edge-label-wrapper"
>
  <div
    class="edge-label"
    class:selected
    class:active={isActive}
    style:--condition-color={style.color}
  >
    <button
      class="condition-badge"
      onclick={() => showConditionPicker = !showConditionPicker}
      title="Click to change condition"
    >
      <span class="condition-icon" class:success={condition === 'success'} class:failure={condition === 'failure'} class:always={condition === 'always'}>
        {#if condition === 'success'}
          ok
        {:else if condition === 'failure'}
          err
        {:else}
          ->
        {/if}
      </span>
    </button>

    <!-- Condition Picker Dropdown -->
    {#if showConditionPicker}
      <div class="condition-picker">
        {#if isEntry}
          <p class="picker-note">
            Entry point - the run starts here, so there is no condition to set.
          </p>
        {:else}
        <button
          class="picker-option success"
          class:active={condition === 'success'}
          onclick={() => setCondition('success')}
        >
          <span class="option-icon">ok</span>
          <span class="option-label">On Success</span>
        </button>
        <button
          class="picker-option failure"
          class:active={condition === 'failure'}
          onclick={() => setCondition('failure')}
        >
          <span class="option-icon">err</span>
          <span class="option-label">On Failure</span>
        </button>
        <button
          class="picker-option always"
          class:active={condition === 'always'}
          onclick={() => setCondition('always')}
        >
          <span class="option-icon">-></span>
          <span class="option-label">Always</span>
        </button>
        {/if}
        <hr class="picker-divider" />
        <button
          class="picker-option delete"
          onclick={() => { showConditionPicker = false; actions?.deleteEdge(id); }}
        >
          <span class="option-icon">×</span>
          <span class="option-label">{isEntry ? 'Remove Entry Point' : 'Delete Edge'}</span>
        </button>
      </div>
    {/if}
  </div>
</EdgeLabel>

<style>
  .condition-edge {
    opacity: 0.8;
    transition: opacity 0.2s ease;
  }

  .condition-edge:hover,
  .condition-edge.active {
    opacity: 1;
  }

  .condition-edge.completed {
    opacity: 0.6;
  }

  .flow-particle {
    filter: drop-shadow(0 0 4px currentColor);
  }

  .edge-label {
    z-index: 1000;
  }

  .condition-badge {
    background: var(--surface-color);
    border: 2px solid var(--condition-color);
    border-radius: 12px;
    padding: 2px 8px;
    cursor: pointer;
    transition: all 0.15s ease;
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .condition-badge:hover {
    background: var(--hover-color);
    transform: scale(1.1);
  }

  .edge-label.selected .condition-badge {
    box-shadow: 0 0 0 2px var(--condition-color);
  }

  .edge-label.active .condition-badge {
    animation: badge-pulse 1s ease-in-out infinite;
  }

  @keyframes badge-pulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.1); }
  }

  .condition-icon {
    font-size: 10px;
    font-weight: bold;
    font-family: monospace;
  }

  .condition-icon.success { color: var(--success-color); }
  .condition-icon.failure { color: var(--error-color); }
  .condition-icon.always { color: var(--primary-color); }

  /* Condition Picker */
  .condition-picker {
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    margin-bottom: 8px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 4px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    z-index: 1001;
    min-width: 120px;
  }

  .picker-note {
    margin: 0;
    padding: 6px 10px;
    max-width: 180px;
    color: var(--text-muted);
    font-size: 11px;
    line-height: 1.35;
  }

  .picker-option {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border: none;
    border-radius: 6px;
    background: transparent;
    cursor: pointer;
    transition: background 0.15s ease;
    text-align: left;
  }

  .picker-option:hover {
    background: var(--hover-color);
  }

  .picker-option.active {
    background: var(--surface-alt);
  }

  .picker-option.success .option-icon { color: var(--success-color); }
  .picker-option.failure .option-icon { color: var(--error-color); }
  .picker-option.always .option-icon { color: var(--primary-color); }

  .option-icon {
    font-size: 11px;
    font-weight: bold;
    font-family: monospace;
    width: 20px;
  }

  .option-label {
    font-size: 12px;
    color: var(--text-color);
  }

  .picker-divider {
    border: none;
    border-top: 1px solid var(--border-color);
    margin: 4px 0;
  }

  .picker-option.delete {
    color: var(--error-color);
  }

  .picker-option.delete .option-icon {
    color: var(--error-color);
  }

  .picker-option.delete:hover {
    background: rgba(255, 100, 100, 0.15);
  }
</style>
