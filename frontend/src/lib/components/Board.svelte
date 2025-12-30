<script lang="ts">
  import { onMount } from 'svelte';
  import { cardsStore, cardsByStatus } from '../stores/cards';
  import { selectedRepoId, selectedRepo } from '../stores/repos';
  import type { Card, CardStatus } from '../api/types';
  import Column from './Column.svelte';
  import CardModal from './CardModal.svelte';

  let selectedCard: Card | null = null;
  let showCreateModal = false;
  let searchQuery = '';

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

  $: filteredCardsByStatus = Object.fromEntries(
    Object.entries($cardsByStatus).map(([status, cards]) => [
      status,
      cards.filter(card => {
        if (!searchQuery.trim()) return true;
        const query = searchQuery.toLowerCase();
        return (
          card.title.toLowerCase().includes(query) ||
          (card.description && card.description.toLowerCase().includes(query)) ||
          (card.branch_name && card.branch_name.toLowerCase().includes(query))
        );
      })
    ])
  ) as typeof $cardsByStatus;

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
        <span class="branch-badge"><span class="pin-icon">📍</span> {$selectedRepo.default_branch}</span>
      </div>
      <div class="board-actions">
        <input
          type="text"
          class="search-input"
          placeholder="Search cards..."
          bind:value={searchQuery}
        />
        <button class="btn-create" on:click={() => showCreateModal = true}>
          + New Card
        </button>
      </div>
    </div>

    <div class="board">
      {#each columns as { status, title }}
        <Column
          {status}
          {title}
          cards={filteredCardsByStatus[status]}
          on:cardclick={handleCardClick}
          on:drop={handleDrop}
        />
      {/each}
    </div>

    {#if filteredCardsByStatus.failed.length > 0}
      <div class="failed-section">
        <h2>Failed</h2>
        <div class="failed-cards">
          <Column
            status="failed"
            title="Failed"
            cards={filteredCardsByStatus.failed}
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
    padding: 1.5rem 2rem;
    overflow: hidden;
    width: 100%;
    min-width: 0;
  }

  @media (min-width: 1800px) {
    .board-container {
      padding: 2rem 3rem;
    }
    min-height: 0;
  }

  @media (max-width: 1200px) {
    .board-container {
      padding: 1rem 1.5rem;
    }
  }

  @media (max-width: 768px) {
    .board-container {
      padding: 1rem;
    }
  }

  .board-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
    gap: 1rem;
  }

  .board-info {
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
  }

  .board-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .search-input {
    padding: 0.7rem 1rem;
    background: var(--input-bg, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.95rem;
    min-width: 200px;
    transition: all 0.2s ease;
  }

  .search-input:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
    box-shadow: 0 0 0 3px rgba(137, 180, 250, 0.1);
  }

  .search-input::placeholder {
    color: var(--text-muted, #6c7086);
  }

  .board-info h1 {
    margin: 0;
    font-size: 1.75rem;
    font-weight: 700;
    color: var(--text-color, #cdd6f4);
    background: linear-gradient(135deg, var(--primary-color, #89b4fa) 0%, var(--text-color, #cdd6f4) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }

  .branch-badge {
    padding: 0.4rem 0.9rem;
    background: var(--surface-color, #313244);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 20px;
    font-size: 0.85rem;
    color: var(--text-color, #cdd6f4);
    font-weight: 500;
    transition: all 0.2s ease;
  }

  .branch-badge:hover {
    background: var(--hover-color, #45475a);
    transform: translateY(-1px);
  }

  .pin-icon {
    filter: hue-rotate(200deg) saturate(0.8);
  }

  .btn-create {
    padding: 0.7rem 1.4rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border: none;
    border-radius: 8px;
    font-weight: 600;
    font-size: 0.95rem;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 8px rgba(137, 180, 250, 0.3);
  }

  .btn-create:hover {
    background: var(--primary-color, #89b4fa);
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(137, 180, 250, 0.4);
  }

  .btn-create:active {
    transform: translateY(0);
  }

  .board {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1.5rem;
    flex: 1;
    overflow-x: auto;
    padding-bottom: 1rem;
    align-items: stretch;
    min-height: 0;
    min-width: 0;
  }

  @media (min-width: 1800px) {
    .board {
      gap: 2rem;
    }
  }

  @media (max-width: 1400px) {
    .board {
      grid-template-columns: repeat(4, minmax(260px, 1fr));
      gap: 1.25rem;
    }
  }

  @media (max-width: 1100px) {
    .board {
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
    }
  }

  @media (max-width: 768px) {
    .board {
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 0.75rem;
    }
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
