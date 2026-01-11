<script lang="ts">
  import { BaseEdge, EdgeLabel, getBezierPath, type EdgeProps } from '@xyflow/svelte';
  import type { EdgeCondition } from '../../api/types';

  interface Props extends EdgeProps {
    data?: {
      condition: EdgeCondition;
      isActive?: boolean;
      isCompleted?: boolean;
      onConditionChange?: (condition: EdgeCondition) => void;
    };
  }

  let {
    id,
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

  // Get path and label position
  let [edgePath, labelX, labelY] = $derived(
    getBezierPath({
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

  // Condition selector dropdown
  let showConditionPicker = $state(false);

  function setCondition(newCondition: EdgeCondition) {
    data?.onConditionChange?.(newCondition);
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
</style>
