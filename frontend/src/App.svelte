<script lang="ts">
  import RepoSelector from './lib/components/RepoSelector.svelte';
  import RunnerPanel from './lib/components/RunnerPanel.svelte';
  import Board from './lib/components/Board.svelte';
</script>

<div class="app">
  <aside class="sidebar">
    <div class="logo">
      <span class="logo-icon">😴</span>
      <span class="logo-text">LazyAF</span>
    </div>
    <RepoSelector />
    <RunnerPanel />
  </aside>

  <main class="main">
    <Board />
  </main>
</div>

<style>
  :global(*) {
    box-sizing: border-box;
  }

  :global(html) {
    scroll-behavior: smooth;
  }

  :global(body) {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    background: var(--bg-color);
    color: var(--text-color);
    overflow: hidden;
  }

  :global(*::-webkit-scrollbar) {
    width: 10px;
    height: 10px;
  }

  :global(*::-webkit-scrollbar-track) {
    background: var(--surface-alt, #181825);
    border-radius: 5px;
  }

  :global(*::-webkit-scrollbar-thumb) {
    background: var(--border-color, #45475a);
    border-radius: 5px;
    transition: background 0.2s ease;
  }

  :global(*::-webkit-scrollbar-thumb:hover) {
    background: var(--primary-color, #89b4fa);
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

  :global(*:focus-visible) {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 4px;
  }

  :global(button), :global(a) {
    transition: all 0.2s ease;
  }

  .app {
    display: flex;
    height: 100vh;
    overflow: hidden;
  }

  .sidebar {
    width: 280px;
    min-width: 280px;
    max-width: 320px;
    background: var(--surface-color);
    border-right: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    flex-shrink: 0;
  }

  @media (max-width: 1200px) {
    .sidebar {
      width: 260px;
      min-width: 260px;
    }
  }

  @media (max-width: 768px) {
    .sidebar {
      width: 240px;
      min-width: 240px;
    }
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
    background-image:
      radial-gradient(at 0% 0%, rgba(137, 180, 250, 0.03) 0px, transparent 50%),
      radial-gradient(at 100% 100%, rgba(166, 227, 161, 0.03) 0px, transparent 50%);
    min-width: 0;
  }
</style>
