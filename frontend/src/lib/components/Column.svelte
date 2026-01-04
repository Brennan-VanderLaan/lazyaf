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
  data-column={status}
  data-testid="column"
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
    border: 1px solid var(--border-color, #45475a);
    border-radius: 12px;
    padding: 1rem;
    min-width: 240px;
    display: flex;
    flex-direction: column;
    max-height: 100%;
    height: 100%;
    transition: all 0.2s ease;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
  }

  .column:hover {
    border-color: var(--primary-color, #89b4fa);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  }

  .column.drag-over {
    background: var(--hover-color, #1e1e2e);
    outline: 2px dashed var(--primary-color, #89b4fa);
    outline-offset: -2px;
    transform: scale(1.02);
  }

  .column-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    padding-bottom: 0.75rem;
    border-bottom: 2px solid var(--border-color, #45475a);
  }

  .column-header h3 {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 700;
    color: var(--text-color, #cdd6f4);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .count {
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    padding: 0.25rem 0.6rem;
    border-radius: 12px;
    font-size: 0.8rem;
    font-weight: 700;
    min-width: 24px;
    text-align: center;
  }

  .card-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    overflow-y: auto;
    flex: 1;
    padding: 0.25rem;
    margin: -0.25rem;
  }

  .card-list::-webkit-scrollbar {
    width: 8px;
  }

  .card-list::-webkit-scrollbar-track {
    background: var(--surface-alt, #181825);
    border-radius: 4px;
  }

  .card-list::-webkit-scrollbar-thumb {
    background: var(--border-color, #45475a);
    border-radius: 4px;
  }

  .card-list::-webkit-scrollbar-thumb:hover {
    background: var(--primary-color, #89b4fa);
  }
</style>
