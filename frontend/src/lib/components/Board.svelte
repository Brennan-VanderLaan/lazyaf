<script lang="ts">
  import { onMount } from 'svelte';
  import { cardsStore, cardsByStatus } from '../stores/cards';
  import { selectedRepoId, selectedRepo } from '../stores/repos';
  import type { Card, CardStatus } from '../api/types';
  import Column from './Column.svelte';
  import CardModal from './CardModal.svelte';

  let selectedCard: Card | null = null;
  let showCreateModal = false;

  const columns: { status: CardStatus; title: string }[] = [
    { status: 'todo', title: 'To Do' },
    { status: 'in_progress', title: 'In Progress' },
    { status: 'in_review', title: 'In Review' },
    { status: 'done', title: 'Done' },
  ];

  $: if ($selectedRepoId) {
    cardsStore.load($selectedRepoId);
  } else {
    cardsStore.clear();
  }

  async function handleDrop(e: CustomEvent<{ cardId: string; status: CardStatus }>) {
    const { cardId, status } = e.detail;
    const card = $cardsStore.find(c => c.id === cardId);
    if (card && card.status !== status) {
      await cardsStore.update(cardId, { status });
    }
  }

  function handleCardClick(e: CustomEvent<Card>) {
    selectedCard = e.detail;
  }
</script>

<div class="board-container">
  {#if $selectedRepo}
    <div class="board-header">
      <div class="board-info">
        <h1>{$selectedRepo.name}</h1>
        <span class="branch-badge">📍 {$selectedRepo.default_branch}</span>
      </div>
      <button class="btn-create" on:click={() => showCreateModal = true}>
        + New Card
      </button>
    </div>

    <div class="board">
      {#each columns as { status, title }}
        <Column
          {status}
          {title}
          cards={$cardsByStatus[status]}
          on:cardclick={handleCardClick}
          on:drop={handleDrop}
        />
      {/each}
    </div>

    {#if $cardsByStatus.failed.length > 0}
      <div class="failed-section">
        <h2>Failed</h2>
        <div class="failed-cards">
          <Column
            status="failed"
            title="Failed"
            cards={$cardsByStatus.failed}
            on:cardclick={handleCardClick}
            on:drop={handleDrop}
          />
        </div>
      </div>
    {/if}
  {:else}
    <div class="no-repo">
      <div class="no-repo-content">
        <span class="no-repo-icon">📁</span>
        <h2>No Repository Selected</h2>
        <p>Select a repository from the sidebar or add a new one to get started.</p>
      </div>
    </div>
  {/if}
</div>

{#if showCreateModal && $selectedRepoId}
  <CardModal
    repoId={$selectedRepoId}
    on:close={() => showCreateModal = false}
    on:created={() => showCreateModal = false}
  />
{/if}

{#if selectedCard}
  <CardModal
    repoId={selectedCard.repo_id}
    card={selectedCard}
    on:close={() => selectedCard = null}
    on:updated={() => selectedCard = null}
    on:deleted={() => selectedCard = null}
  />
{/if}

<style>
  .board-container {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 1.0rem;
    overflow: hidden;
  }

  .board-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.5rem;
  }

  .board-info {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .board-info h1 {
    margin: 0;
    font-size: 1.5rem;
    color: var(--text-color, #cdd6f4);
  }

  .branch-badge {
    padding: 0.25rem 0.75rem;
    background: var(--surface-color, #313244);
    border-radius: 20px;
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .btn-create {
    padding: 0.6rem 1.2rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border: none;
    border-radius: 6px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }

  .btn-create:hover {
    opacity: 0.9;
  }

  .board {
    display: flex;
    gap: 1rem;
    flex: 1;
    overflow-x: auto;
    padding-bottom: 1rem;
  }

  .failed-section {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color, #45475a);
  }

  .failed-section h2 {
    margin: 0 0 1rem;
    font-size: 1rem;
    color: var(--error-color, #f38ba8);
  }

  .failed-cards {
    display: flex;
  }

  .no-repo {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .no-repo-content {
    text-align: center;
    color: var(--text-muted, #6c7086);
  }

  .no-repo-icon {
    font-size: 4rem;
    display: block;
    margin-bottom: 1rem;
  }

  .no-repo-content h2 {
    margin: 0 0 0.5rem;
    color: var(--text-color, #cdd6f4);
  }

  .no-repo-content p {
    margin: 0;
  }
</style>
