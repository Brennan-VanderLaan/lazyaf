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
    border-left: 3px solid var(--status-color);
    border-radius: 6px;
    padding: 0.75rem;
    cursor: grab;
    transition: transform 0.15s, box-shadow 0.15s;
  }

  .card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  }

  .card:active {
    cursor: grabbing;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 0.5rem;
  }

  .card-title {
    margin: 0;
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color, #cdd6f4);
    line-height: 1.3;
  }

  .pr-link {
    flex-shrink: 0;
    padding: 0.15rem 0.4rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 600;
    text-decoration: none;
  }

  .pr-link:hover {
    opacity: 0.9;
  }

  .card-description {
    margin: 0.5rem 0 0;
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .card-footer {
    margin-top: 0.5rem;
  }

  .branch-name {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    font-family: monospace;
  }
</style>
