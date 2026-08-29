<script lang="ts">
  import { onMount } from 'svelte';
  import { agentFilesStore } from '../stores/agentFiles';
  import AgentFileModal from './AgentFileModal.svelte';
  import type { AgentFile } from '../api/types';

  let showAgents = true;
  let showModal = false;
  let selectedAgent: AgentFile | null = null;

  onMount(() => {
    agentFilesStore.load();
  });

  function openNewModal() {
    selectedAgent = null;
    showModal = true;
  }

  function openEditModal(agent: AgentFile) {
    selectedAgent = agent;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    selectedAgent = null;
  }

  function handleCreated() {
    closeModal();
  }

  function handleUpdated() {
    closeModal();
  }

  function handleDeleted() {
    closeModal();
  }
</script>

<div class="agent-panel" data-testid="agent-panel">
  <div class="panel-header">
    <h2>Agent Files</h2>
    <button class="btn-icon" data-testid="add-agent-btn" on:click={openNewModal} title="Create new agent file">
      +
    </button>
  </div>

  <button
    class="btn-toggle"
    on:click={() => showAgents = !showAgents}
  >
    {showAgents ? '▼' : '▶'} Agents ({$agentFilesStore.length})
  </button>

  {#if showAgents}
    <div class="agent-list" data-testid="agent-list">
      {#each $agentFilesStore as agent (agent.id)}
        <button
          class="agent-item"
          data-testid="agent-item"
          data-agent-id={agent.id}
          on:click={() => openEditModal(agent)}
        >
          <span class="agent-name">{agent.name}</span>
          {#if agent.description}
            <span class="agent-description">{agent.description}</span>
          {/if}
        </button>
      {:else}
        <div class="no-agents">
          <p>No agent files yet</p>
          <p class="hint">Click + to create your first agent</p>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showModal}
  <AgentFileModal
    agentFile={selectedAgent}
    on:close={closeModal}
    on:created={handleCreated}
    on:updated={handleUpdated}
    on:deleted={handleDeleted}
  />
{/if}

<style>
  .agent-panel {
    background: var(--surface-color, #1e1e2e);
    border-radius: 8px;
    padding: 1rem;
    margin-top: 1rem;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .panel-header h2 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--text-color, #cdd6f4);
  }

  .btn-icon {
    background: none;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    cursor: pointer;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.2rem;
    font-weight: 300;
  }

  .btn-icon:hover {
    background: var(--hover-color, #313244);
  }

  .btn-toggle {
    width: 100%;
    padding: 0.5rem;
    background: none;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    color: var(--text-muted, #6c7086);
    font-size: 0.8rem;
    cursor: pointer;
    text-align: left;
  }

  .btn-toggle:hover {
    background: var(--hover-color, #313244);
  }

  .agent-list {
    margin-top: 0.5rem;
    max-height: 300px;
    overflow-y: auto;
  }

  .agent-item {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    padding: 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    width: 100%;
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    color: inherit;
  }

  .agent-item:hover {
    background: var(--hover-color, #313244);
  }

  .agent-name {
    font-family: monospace;
    color: var(--text-color, #cdd6f4);
    font-weight: 500;
  }

  .agent-description {
    color: var(--text-muted, #6c7086);
    font-size: 0.75rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .no-agents {
    padding: 1rem;
    text-align: center;
    color: var(--text-muted, #6c7086);
  }

  .no-agents p {
    margin: 0 0 0.25rem;
  }

  .no-agents .hint {
    font-size: 0.8rem;
    opacity: 0.7;
  }
</style>
