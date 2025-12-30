<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { Card as CardType, CardStatus } from '../api/types';
  import Card from './Card.svelte';

  export let status: CardStatus;
  export let title: string;
  export let cards: CardType[];

  const dispatch = createEventDispatcher<{
    cardclick: CardType;
    drop: { cardId: string; status: CardStatus };
  }>();

  let isDragOver = false;

  function handleDragOver(e: DragEvent) {
    e.preventDefault();
    if (e.dataTransfer) {
      e.dataTransfer.dropEffect = 'move';
    }
    isDragOver = true;
  }

  function handleDragLeave() {
    isDragOver = false;
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    isDragOver = false;
    const cardId = e.dataTransfer?.getData('text/plain');
    if (cardId) {
      dispatch('drop', { cardId, status });
    }
  }
</script>

<div
  class="column"
  class:drag-over={isDragOver}
  on:dragover={handleDragOver}
  on:dragleave={handleDragLeave}
  on:drop={handleDrop}
  role="region"
  aria-label="{title} column"
>
  <div class="column-header">
    <h3>{title}</h3>
    <span class="count">{cards.length}</span>
  </div>

  <div class="card-list">
    {#each cards as card (card.id)}
      <Card {card} on:click={() => dispatch('cardclick', card)} />
    {/each}
  </div>
</div>

<style>
  .column {
    background: var(--surface-color, #181825);
    border-radius: 8px;
    padding: 0.75rem;
    min-width: 240px;
    flex: 1;
    display: flex;
    flex-direction: column;
    max-height: 100%;
    transition: background 0.15s;
  }

  .column.drag-over {
    background: var(--hover-color, #1e1e2e);
    outline: 2px dashed var(--primary-color, #89b4fa);
  }

  .column-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .column-header h3 {
    margin: 0;
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text-color, #cdd6f4);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .count {
    background: var(--badge-bg, #313244);
    color: var(--text-muted, #6c7086);
    padding: 0.15rem 0.5rem;
    border-radius: 10px;
    font-size: 0.75rem;
    font-weight: 500;
  }

  .card-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    overflow-y: auto;
    flex: 1;
  }
</style>
