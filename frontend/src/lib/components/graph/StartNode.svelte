<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';

  interface Props {
    data: {
      label?: string;
    };
    selected?: boolean;
  }

  let { data, selected = false }: Props = $props();
</script>

<div class="start-node" class:selected>
  <div class="start-icon">
    <svg viewBox="0 0 24 24" fill="currentColor">
      <path d="M8 5v14l11-7z"/>
    </svg>
  </div>
  <span class="start-label">{data.label || 'Start'}</span>

  <!-- Output Handle - can connect to multiple nodes -->
  <Handle type="source" position={Position.Right} />
</div>

<style>
  .start-node {
    background: linear-gradient(135deg, var(--success-color) 0%, #2dd4bf 100%);
    border: 2px solid var(--success-color);
    border-radius: 50%;
    width: 64px;
    height: 64px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    cursor: default;
    transition: all 0.2s ease;
    box-shadow: 0 4px 12px rgba(166, 227, 161, 0.3);
  }

  .start-node.selected {
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(137, 180, 250, 0.4), 0 4px 12px rgba(166, 227, 161, 0.3);
  }

  .start-node:hover {
    transform: scale(1.05);
    box-shadow: 0 6px 16px rgba(166, 227, 161, 0.4);
  }

  .start-icon {
    width: 24px;
    height: 24px;
    color: var(--primary-text);
    pointer-events: none;
  }

  .start-icon svg {
    width: 100%;
    height: 100%;
    pointer-events: none;
  }

  .start-label {
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--primary-text);
    pointer-events: none;
  }

  /* Position the handle on the right edge of the circle */
  :global(.start-node .svelte-flow__handle) {
    width: 12px;
    height: 12px;
    background: var(--primary-text);
    border: 2px solid var(--success-color);
  }

  :global(.start-node .svelte-flow__handle:hover) {
    background: var(--primary-color);
    transform: scale(1.2);
  }
</style>
