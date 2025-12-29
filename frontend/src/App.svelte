<script lang="ts">
  import { onMount } from 'svelte';
  import RepoSelector from './lib/components/RepoSelector.svelte';
  import Board from './lib/components/Board.svelte';
  import { websocketStore } from './lib/stores/websocket';

  onMount(() => {
    websocketStore.connect();
    return () => websocketStore.disconnect();
  });
</script>

<div class="app">
  <aside class="sidebar">
    <div class="logo">
      <span class="logo-icon">😴</span>
      <span class="logo-text">LazyAF</span>
    </div>
    <RepoSelector />
  </aside>

  <main class="main">
    <Board />
  </main>
</div>

<style>
  :global(*) {
    box-sizing: border-box;
  }

  :global(body) {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    background: var(--bg-color);
    color: var(--text-color);
  }

  :global(:root) {
    --bg-color: #11111b;
    --surface-color: #1e1e2e;
    --surface-alt: #181825;
    --border-color: #45475a;
    --hover-color: #313244;
    --selected-color: #45475a;
    --text-color: #cdd6f4;
    --text-muted: #6c7086;
    --primary-color: #89b4fa;
    --primary-text: #1e1e2e;
    --error-color: #f38ba8;
    --success-color: #a6e3a1;
    --warning-color: #f9e2af;
    --input-bg: #1e1e2e;
    --badge-bg: #313244;
    --card-bg: #1e1e2e;
  }

  .app {
    display: flex;
    height: 100vh;
    overflow: hidden;
  }

  .sidebar {
    width: 300px;
    background: var(--surface-color);
    border-right: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 1.25rem 1rem;
    border-bottom: 1px solid var(--border-color);
  }

  .logo-icon {
    font-size: 1.75rem;
  }

  .logo-text {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-color);
  }

  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg-color);
  }
</style>
