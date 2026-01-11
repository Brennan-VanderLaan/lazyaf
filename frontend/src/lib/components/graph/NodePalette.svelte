<script lang="ts">
  import type { StepType } from '../../api/types';

  interface Props {
    onDropStep: (type: StepType, position: { x: number; y: number }) => void;
  }

  let { onDropStep }: Props = $props();

  // Node type definitions
  const nodeTypes: { type: StepType; label: string; icon: string; color: string; description: string }[] = [
    {
      type: 'script',
      label: 'Script',
      icon: '>_',
      color: 'var(--primary-color)',
      description: 'Run a shell command',
    },
    {
      type: 'docker',
      label: 'Docker',
      icon: '[]',
      color: '#2496ed',
      description: 'Run in a Docker container',
    },
    {
      type: 'agent',
      label: 'AI Agent',
      icon: '*',
      color: '#a855f7',
      description: 'Run an AI agent task',
    },
  ];

  // Drag state
  let draggingType = $state<StepType | null>(null);

  function onDragStart(event: DragEvent, type: StepType) {
    if (!event.dataTransfer) return;
    event.dataTransfer.setData('application/pipeline-node', type);
    event.dataTransfer.effectAllowed = 'copy';
    draggingType = type;
  }

  function onDragEnd() {
    draggingType = null;
  }
</script>

<div class="node-palette">
  <div class="palette-header">
    <span class="palette-title">Add Steps</span>
    <span class="palette-hint">Drag to canvas</span>
  </div>

  <div class="palette-items">
    {#each nodeTypes as node}
      <div
        class="palette-item"
        class:dragging={draggingType === node.type}
        style:--node-color={node.color}
        draggable="true"
        ondragstart={(e) => onDragStart(e, node.type)}
        ondragend={onDragEnd}
        role="button"
        tabindex="0"
      >
        <div class="item-icon" style:background={node.color}>
          <span>{node.icon}</span>
        </div>
        <div class="item-info">
          <span class="item-label">{node.label}</span>
          <span class="item-desc">{node.description}</span>
        </div>
        <div class="drag-handle">
          <span class="handle-dots"></span>
        </div>
      </div>
    {/each}
  </div>

  <div class="palette-footer">
    <div class="tip">
      <span class="tip-icon">i</span>
      <span class="tip-text">Double-click node to edit</span>
    </div>
  </div>
</div>

<style>
  .node-palette {
    width: 200px;
    background: var(--surface-color);
    border-right: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .palette-header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .palette-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-color);
  }

  .palette-hint {
    font-size: 11px;
    color: var(--text-muted);
  }

  .palette-items {
    flex: 1;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow-y: auto;
  }

  .palette-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px;
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    cursor: grab;
    transition: all 0.15s ease;
  }

  .palette-item:hover {
    border-color: var(--node-color);
    background: var(--hover-color);
  }

  .palette-item:active,
  .palette-item.dragging {
    cursor: grabbing;
    opacity: 0.7;
    transform: scale(0.98);
  }

  .item-icon {
    width: 32px;
    height: 32px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .item-icon span {
    color: var(--primary-text);
    font-weight: bold;
    font-size: 13px;
    font-family: monospace;
  }

  .item-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .item-label {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-color);
  }

  .item-desc {
    font-size: 10px;
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .drag-handle {
    width: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0.5;
  }

  .handle-dots {
    width: 8px;
    height: 14px;
    background-image: radial-gradient(circle, var(--text-muted) 1.5px, transparent 1.5px);
    background-size: 4px 4px;
  }

  .palette-item:hover .drag-handle {
    opacity: 1;
  }

  .palette-footer {
    padding: 12px 16px;
    border-top: 1px solid var(--border-color);
  }

  .tip {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .tip-icon {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--surface-alt);
    color: var(--text-muted);
    font-size: 10px;
    font-weight: bold;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .tip-text {
    font-size: 11px;
    color: var(--text-muted);
  }
</style>
