<script lang="ts">
  import { onMount } from 'svelte';
  import type { StepType } from '../../api/types';

  interface Props {
    x: number;
    y: number;
    onAddStep: (type: StepType) => void;
    onClose: () => void;
  }

  let { x, y, onAddStep, onClose }: Props = $props();

  // Menu items
  const menuItems: { type: StepType; label: string; icon: string; color: string; description: string }[] = [
    { type: 'script', label: 'Add Script Step', icon: '>_', color: 'var(--primary-color)', description: 'Run a shell command' },
    { type: 'docker', label: 'Add Docker Step', icon: '[]', color: '#2496ed', description: 'Run in Docker container' },
    { type: 'agent', label: 'Add AI Agent Step', icon: '*', color: '#a855f7', description: 'Run an AI agent' },
  ];

  // Handle click outside to close
  function handleClickOutside(event: MouseEvent) {
    const target = event.target as HTMLElement;
    if (!target.closest('.context-menu')) {
      onClose();
    }
  }

  // ESCAPE IS DELIBERATELY NOT HANDLED HERE. It used to be, on this same
  // deferred `document` listener - and it did not work: the `setTimeout` is a
  // macrotask, opening the menu queues a SvelteFlow re-render ahead of it, and
  // in the gap between "the menu is visible" and "the listener exists" an
  // Escape lands on nothing. Measured, not guessed: Escape pressed straight
  // after the menu appeared left it open every time; Escape after a 500ms
  // pause closed it. A key binding whose behaviour depends on how fast you
  // press the key is the shape of a bug that gets fixed and reappears.
  //
  // `PipelineGraphEditor` owns dismissal now, from a `svelte:window` handler
  // bound when the EDITOR mounts - long before any overlay can open, so there
  // is no window to lose. The footer's "ESC to close" hint is still true.
  //
  // Click-outside stays here, because the deferral is LOAD-BEARING for it:
  // without it the very click that opens the menu closes it again.
  onMount(() => {
    const timer = setTimeout(() => {
      document.addEventListener('click', handleClickOutside);
    }, 0);

    return () => {
      clearTimeout(timer);
      document.removeEventListener('click', handleClickOutside);
    };
  });

  function handleItemClick(type: StepType) {
    onAddStep(type);
    onClose();
  }
</script>

<div
  class="context-menu"
  style="left: {x}px; top: {y}px;"
  role="menu"
>
  <div class="menu-header">
    <span>Add Step</span>
  </div>

  <div class="menu-items">
    {#each menuItems as item}
      <button
        class="menu-item"
        style:--item-color={item.color}
        onclick={() => handleItemClick(item.type)}
        role="menuitem"
      >
        <div class="item-icon" style:background={item.color}>
          <span>{item.icon}</span>
        </div>
        <div class="item-content">
          <span class="item-label">{item.label}</span>
          <span class="item-desc">{item.description}</span>
        </div>
      </button>
    {/each}
  </div>

  <div class="menu-footer">
    <span class="footer-hint">ESC to close</span>
  </div>
</div>

<style>
  .context-menu {
    position: fixed;
    z-index: 10000;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    min-width: 220px;
    overflow: hidden;
    animation: menu-appear 0.15s ease-out;
  }

  @keyframes menu-appear {
    from {
      opacity: 0;
      transform: scale(0.95) translateY(-5px);
    }
    to {
      opacity: 1;
      transform: scale(1) translateY(0);
    }
  }

  .menu-header {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border-color);
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .menu-items {
    padding: 6px;
  }

  .menu-item {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 10px;
    border: none;
    border-radius: 8px;
    background: transparent;
    cursor: pointer;
    transition: background 0.1s ease;
    text-align: left;
  }

  .menu-item:hover {
    background: var(--hover-color);
  }

  .menu-item:active {
    background: var(--surface-alt);
  }

  .item-icon {
    width: 32px;
    height: 32px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: transform 0.15s ease;
  }

  .menu-item:hover .item-icon {
    transform: scale(1.1);
  }

  .item-icon span {
    color: var(--primary-text);
    font-weight: bold;
    font-size: 13px;
    font-family: monospace;
  }

  .item-content {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .item-label {
    font-size: 13px;
    font-weight: 500;
    color: var(--text-color);
  }

  .item-desc {
    font-size: 11px;
    color: var(--text-muted);
  }

  .menu-footer {
    padding: 8px 14px;
    border-top: 1px solid var(--border-color);
    background: var(--surface-alt);
  }

  .footer-hint {
    font-size: 10px;
    color: var(--text-muted);
  }
</style>
