<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { Card } from '../api/types';

  export let card: Card;

  const dispatch = createEventDispatcher<{
    click: Card;
    dragstart: Card;
  }>();

  function handleDragStart(e: DragEvent) {
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', card.id);
    }
    dispatch('dragstart', card);
  }

  const statusColors: Record<string, string> = {
    todo: '#89b4fa',
    in_progress: '#f9e2af',
    in_review: '#cba6f7',
    done: '#a6e3a1',
    failed: '#f38ba8',
  };
</script>

<div
  class="card"
  draggable="true"
  on:dragstart={handleDragStart}
  on:click={() => dispatch('click', card)}
  on:keydown={(e) => e.key === 'Enter' && dispatch('click', card)}
  role="button"
  tabindex="0"
  style="--status-color: {statusColors[card.status]}"
>
  <div class="card-header">
    <h3 class="card-title">{card.title}</h3>
    {#if card.pr_url}
      <a href={card.pr_url} target="_blank" rel="noopener" class="pr-link" on:click|stopPropagation>
        PR
      </a>
    {/if}
  </div>

  {#if card.description}
    <p class="card-description">{card.description}</p>
  {/if}

  <div class="card-footer">
    {#if card.branch_name}
      <span class="branch-name">🌿 {card.branch_name}</span>
    {/if}
  </div>
</div>

<style>
  .card {
    background: var(--card-bg, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-left: 4px solid var(--status-color);
    border-radius: 8px;
    padding: 1rem;
    cursor: grab;
    transition: all 0.2s ease;
    position: relative;
    overflow: hidden;
  }

  .card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: linear-gradient(135deg, var(--status-color) 0%, transparent 50%);
    opacity: 0;
    transition: opacity 0.2s ease;
    pointer-events: none;
  }

  .card:hover::before {
    opacity: 0.05;
  }

  .card:hover {
    transform: translateY(-3px);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4);
    border-color: var(--status-color);
  }

  .card:active {
    cursor: grabbing;
    transform: translateY(-1px);
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 0.75rem;
    position: relative;
    z-index: 1;
  }

  .card-title {
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-color, #cdd6f4);
    line-height: 1.4;
    word-wrap: break-word;
  }

  .pr-link {
    flex-shrink: 0;
    padding: 0.25rem 0.6rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 700;
    text-decoration: none;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(137, 180, 250, 0.3);
  }

  .pr-link:hover {
    background: var(--primary-color, #89b4fa);
    transform: scale(1.05);
    box-shadow: 0 3px 8px rgba(137, 180, 250, 0.5);
  }

  .card-description {
    margin: 0.75rem 0 0;
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
    line-height: 1.5;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
    position: relative;
    z-index: 1;
  }

  .card-footer {
    margin-top: 0.75rem;
    position: relative;
    z-index: 1;
  }

  .branch-name {
    display: inline-block;
    padding: 0.3rem 0.6rem;
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    font-size: 0.75rem;
    color: var(--success-color, #a6e3a1);
    font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, 'Courier New', monospace;
    font-weight: 500;
    transition: all 0.2s ease;
  }

  .branch-name:hover {
    background: var(--hover-color, #313244);
    border-color: var(--success-color, #a6e3a1);
  }
</style>
