<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';
  import type { PipelineStepV2, RunStatus, StepType } from '../../api/types';

  interface Props {
    data: {
      step: PipelineStepV2;
      status?: RunStatus;
      isEntryPoint: boolean;
      isActive?: boolean;
      isCompleted?: boolean;
      onEdit?: () => void;
      onDelete?: () => void;
      onToggleEntryPoint?: () => void;
    };
    selected?: boolean;
  }

  let { data, selected = false }: Props = $props();

  // Collapsed state
  let expanded = $state(false);

  // Step type icons and colors
  const typeConfig: Record<StepType, { icon: string; color: string; label: string }> = {
    script: { icon: '>', color: 'var(--primary-color)', label: 'Script' },
    docker: { icon: '[]', label: 'Docker', color: '#2496ed' },
    agent: { icon: '*', color: '#a855f7', label: 'AI Agent' },
  };

  // Status colors
  const statusColors: Record<RunStatus, string> = {
    pending: 'var(--text-muted)',
    running: 'var(--warning-color)',
    passed: 'var(--success-color)',
    failed: 'var(--error-color)',
    cancelled: 'var(--text-muted)',
  };

  let config = $derived(typeConfig[data.step.type]);
  let statusColor = $derived(data.status ? statusColors[data.status] : null);

  // Get display info for the step
  function getStepDetails(): string[] {
    const details: string[] = [];
    const { type, config: stepConfig } = data.step;

    if (type === 'script' && stepConfig.command) {
      details.push(`$ ${stepConfig.command}`);
    } else if (type === 'docker') {
      if (stepConfig.image) details.push(`Image: ${stepConfig.image}`);
      if (stepConfig.command) details.push(`$ ${stepConfig.command}`);
    } else if (type === 'agent') {
      if (stepConfig.title) details.push(stepConfig.title);
      if (stepConfig.runner_type) details.push(`Runner: ${stepConfig.runner_type}`);
    }

    return details;
  }

  let details = $derived(getStepDetails());
</script>

<div
  class="step-node"
  class:selected
  class:active={data.isActive}
  class:completed={data.isCompleted}
  class:expanded
  style:--type-color={config.color}
  style:--status-color={statusColor}
  role="button"
  tabindex="0"
  ondblclick={() => data.onEdit?.()}
>
  <!-- Input Handle - always shown, connections from Start node determine entry points -->
  <Handle type="target" position={Position.Left} />

  <!-- Status Pulse Animation -->
  {#if data.isActive}
    <div class="pulse-ring"></div>
  {/if}

  <!-- Node Header -->
  <div class="node-header">
    <div class="type-badge" style:background={config.color}>
      <span class="type-icon">{config.icon}</span>
    </div>
    <div class="node-title">
      <span class="name">{data.step.name}</span>
      <span class="type-label">{config.label}</span>
    </div>
    <button
      class="expand-btn"
      onclick={(e) => { e.stopPropagation(); expanded = !expanded; }}
      title={expanded ? 'Collapse' : 'Expand'}
    >
      {expanded ? '-' : '+'}
    </button>
  </div>

  <!-- Status Indicator -->
  {#if data.status}
    <div class="status-bar" style:background={statusColor}>
      <span class="status-text">{data.status}</span>
    </div>
  {/if}

  <!-- Expanded Details -->
  {#if expanded}
    <div class="node-details">
      {#each details as detail}
        <div class="detail-line">{detail}</div>
      {/each}

      {#if data.step.timeout !== 300}
        <div class="detail-line timeout">Timeout: {data.step.timeout}s</div>
      {/if}

      {#if data.step.continue_in_context}
        <div class="detail-line context">Continues in same workspace</div>
      {/if}
    </div>

    <!-- Action Buttons -->
    <div class="node-actions">
      <button class="action-btn edit" onclick={() => data.onEdit?.()} title="Edit Step">
        Edit
      </button>
      <button class="action-btn delete" onclick={() => data.onDelete?.()} title="Delete Step">
        Delete
      </button>
    </div>
  {/if}

  <!-- Output Handle -->
  <Handle type="source" position={Position.Right} />
</div>

<style>
  .step-node {
    background: var(--surface-color);
    border: 2px solid var(--border-color);
    border-radius: 8px;
    min-width: 180px;
    max-width: 280px;
    font-family: inherit;
    position: relative;
    transition: all 0.2s ease;
    cursor: pointer;
  }

  /* Entry point has distinct shape - more rounded, left border accent */
  .step-node.entry-point {
    border-radius: 20px 8px 8px 20px;
    border-left: 4px solid var(--primary-color);
  }

  .step-node.selected {
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(137, 180, 250, 0.3);
  }

  .step-node.active {
    border-color: var(--warning-color);
    box-shadow: 0 0 20px rgba(249, 226, 175, 0.4);
  }

  .step-node.completed {
    border-color: var(--success-color);
  }

  .step-node:hover {
    border-color: var(--type-color, var(--primary-color));
  }

  /* Entry indicator arrow */
  .entry-indicator {
    position: absolute;
    left: -30px;
    top: 50%;
    transform: translateY(-50%);
    display: flex;
    align-items: center;
  }

  .entry-arrow {
    width: 0;
    height: 0;
    border-top: 8px solid transparent;
    border-bottom: 8px solid transparent;
    border-left: 12px solid var(--primary-color);
  }

  /* Pulse animation for active steps */
  .pulse-ring {
    position: absolute;
    inset: -4px;
    border: 2px solid var(--warning-color);
    border-radius: inherit;
    animation: pulse 1.5s ease-out infinite;
    pointer-events: none;
  }

  @keyframes pulse {
    0% {
      transform: scale(1);
      opacity: 1;
    }
    100% {
      transform: scale(1.15);
      opacity: 0;
    }
  }

  /* Header */
  .node-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
  }

  .type-badge {
    width: 28px;
    height: 28px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .type-icon {
    color: var(--primary-text);
    font-weight: bold;
    font-size: 14px;
    font-family: monospace;
  }

  .node-title {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .name {
    color: var(--text-color);
    font-weight: 500;
    font-size: 13px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .type-label {
    color: var(--text-muted);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .expand-btn {
    width: 20px;
    height: 20px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--surface-alt);
    color: var(--text-muted);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    font-weight: bold;
  }

  .expand-btn:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  /* Status bar */
  .status-bar {
    padding: 4px 12px;
    margin: 0 2px;
    border-radius: 4px;
  }

  .status-text {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--primary-text);
    font-weight: 600;
  }

  /* Details (expanded) */
  .node-details {
    padding: 8px 12px;
    border-top: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .detail-line {
    font-size: 11px;
    color: var(--text-muted);
    font-family: monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .detail-line.timeout {
    color: var(--warning-color);
  }

  .detail-line.context {
    color: var(--primary-color);
    font-style: italic;
  }

  /* Actions (expanded) */
  .node-actions {
    display: flex;
    gap: 4px;
    padding: 8px 12px;
    border-top: 1px solid var(--border-color);
  }

  .action-btn {
    flex: 1;
    padding: 4px 8px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--surface-alt);
    color: var(--text-muted);
    font-size: 10px;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .action-btn:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .action-btn.edit:hover {
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .action-btn.entry.active {
    background: var(--primary-color);
    color: var(--primary-text);
    border-color: var(--primary-color);
  }

  .action-btn.delete:hover {
    border-color: var(--error-color);
    color: var(--error-color);
  }
</style>
