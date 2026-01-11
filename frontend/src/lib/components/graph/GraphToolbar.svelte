<script lang="ts">
  import type { StepType } from '../../api/types';

  interface Props {
    onAddStep: (type: StepType) => void;
  }

  let { onAddStep }: Props = $props();

  // Quick add buttons
  const stepTypes: { type: StepType; label: string; icon: string; color: string; shortcut: string }[] = [
    { type: 'script', label: 'Script', icon: '>_', color: 'var(--primary-color)', shortcut: 'S' },
    { type: 'docker', label: 'Docker', icon: '[]', color: '#2496ed', shortcut: 'D' },
    { type: 'agent', label: 'Agent', icon: '*', color: '#a855f7', shortcut: 'A' },
  ];
</script>

<div class="graph-toolbar">
  <div class="toolbar-section">
    <span class="section-label">Quick Add:</span>
    <div class="button-group">
      {#each stepTypes as step}
        <button
          class="add-btn"
          style:--btn-color={step.color}
          onclick={() => onAddStep(step.type)}
          title="{step.label} ({step.shortcut})"
        >
          <span class="btn-icon" style:background={step.color}>{step.icon}</span>
          <span class="btn-label">{step.label}</span>
          <span class="btn-shortcut">{step.shortcut}</span>
        </button>
      {/each}
    </div>
  </div>

  <div class="toolbar-divider"></div>

  <div class="toolbar-section">
    <span class="section-label">Tips:</span>
    <div class="tips">
      <span class="tip">Right-click canvas for menu</span>
      <span class="tip-sep">|</span>
      <span class="tip">Drag from sidebar</span>
      <span class="tip-sep">|</span>
      <span class="tip">Connect nodes by dragging handles</span>
    </div>
  </div>
</div>

<style>
  .graph-toolbar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 10px 16px;
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
  }

  .toolbar-section {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .section-label {
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .button-group {
    display: flex;
    gap: 6px;
  }

  .add-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    background: var(--surface-alt);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .add-btn:hover {
    border-color: var(--btn-color);
    background: var(--hover-color);
  }

  .btn-icon {
    width: 20px;
    height: 20px;
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-weight: bold;
    font-family: monospace;
    color: var(--primary-text);
  }

  .btn-label {
    font-size: 12px;
    color: var(--text-color);
  }

  .btn-shortcut {
    font-size: 10px;
    color: var(--text-muted);
    padding: 2px 5px;
    background: var(--bg-color);
    border-radius: 3px;
    font-family: monospace;
  }

  .toolbar-divider {
    width: 1px;
    height: 24px;
    background: var(--border-color);
  }

  .tips {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .tip {
    font-size: 11px;
    color: var(--text-muted);
  }

  .tip-sep {
    color: var(--border-color);
    font-size: 11px;
  }
</style>
