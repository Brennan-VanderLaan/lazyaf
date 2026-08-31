<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import Router, { location, link } from 'svelte-spa-router';
  import RepoSelector from './lib/components/RepoSelector.svelte';
  import RunnerPanel from './lib/components/RunnerPanel.svelte';
  import AgentPanel from './lib/components/AgentPanel.svelte';
  import BoardPage from './lib/pages/BoardPage.svelte';
  import PipelinesPage from './lib/pages/PipelinesPage.svelte';
  import PipelineEditorPage from './lib/pages/PipelineEditorPage.svelte';
  import PlaygroundPage from './lib/pages/PlaygroundPage.svelte';
  import SpecsPage from './lib/pages/SpecsPage.svelte';
  import ExperimentsPage from './lib/pages/ExperimentsPage.svelte';
  import EndpointsPage from './lib/pages/EndpointsPage.svelte';
  import { get } from 'svelte/store';
  import { websocketStore } from './lib/stores/websocket';
  import { reposStore, selectedRepoId } from './lib/stores/repos';
  import { hasRunningJobs } from './lib/stores/jobs';
  import { hasActiveRuns } from './lib/stores/pipelines';
  import { isRunning as playgroundRunning } from './lib/stores/playground';

  /**
   * Which repository you were looking at, remembered across reloads.
   *
   * `selectedRepoId` is in-memory only, so a refresh - or a restored tab, or
   * following a link - dropped every page in the app back to its "select a
   * repository" dead end with no breadcrumb. Three separate QA lenses filed
   * this against the board, the pipelines page and the playground; it is one
   * fact, so it is remembered in one place.
   *
   * This is deliberately NOT the URL. Putting the repo in the route would
   * make boards shareable and is the better end state, but it is a routing
   * change across every page; this is the additive half that stops the
   * refresh from hurting.
   */
  const SELECTED_REPO_KEY = 'lazyaf.selected-repo';

  /**
   * A browser that refuses storage (private windows, storage disabled) makes
   * us forget the selection - it does not make the app wrong. Nothing is
   * swallowed here except the inability to remember a convenience.
   */
  function rememberSelectedRepo(id: string | null) {
    try {
      if (id) localStorage.setItem(SELECTED_REPO_KEY, id);
      else localStorage.removeItem(SELECTED_REPO_KEY);
    } catch {
      /* storage unavailable - the selection just will not survive a reload */
    }
  }

  function readRememberedRepo(): string | null {
    try {
      return localStorage.getItem(SELECTED_REPO_KEY);
    } catch {
      return null;
    }
  }

  const routes = {
    '/': BoardPage,
    '/pipelines': PipelinesPage,
    '/pipelines/:id/edit': PipelineEditorPage,
    '/playground': PlaygroundPage,
    '/specs': SpecsPage,
    '/experiments': ExperimentsPage,
    '/endpoints': EndpointsPage,
  };

  let stopRemembering: (() => void) | null = null;

  onMount(() => {
    websocketStore.connect();

    // Read BEFORE subscribing: the persist subscription below fires
    // immediately with the current (null) value, which would erase the key.
    const remembered = readRememberedRepo();
    let restored = !remembered;

    // Restore only once the repo is actually in the list. Setting an id that
    // no longer exists would leave every page asking for cards on a deleted
    // repository, which is a worse landing than the empty state.
    const unsubRepos = reposStore.subscribe(repos => {
      if (restored) return;
      if (get(selectedRepoId)) {
        restored = true;   // the user got there first; do not move them
        return;
      }
      if (repos.some(r => r.id === remembered)) {
        restored = true;
        selectedRepoId.set(remembered);
      }
    });

    let primed = false;
    const unsubSelected = selectedRepoId.subscribe(id => {
      if (!primed) {
        primed = true;
        return;
      }
      rememberSelectedRepo(id);
    });

    stopRemembering = () => {
      unsubRepos();
      unsubSelected();
    };
  });

  onDestroy(() => {
    websocketStore.disconnect();
    stopRemembering?.();
  });
</script>

<div class="app">
  <aside class="sidebar">
    <div class="logo">
      <span class="logo-icon">{$hasRunningJobs || $hasActiveRuns ? '⚙️' : '😴'}</span>
      <span class="logo-text">LazyAF</span>
    </div>

    <nav class="nav">
      <a href="/" use:link class="nav-item" class:active={$location === '/'}>
        <span class="nav-icon">📋</span>
        <span class="nav-label">Board</span>
      </a>
      <a href="/pipelines" use:link class="nav-item" class:active={$location === '/pipelines'}>
        <span class="nav-icon">{$hasActiveRuns ? '⚙️' : '🔄'}</span>
        <span class="nav-label">Pipelines</span>
        {#if $hasActiveRuns}
          <span class="nav-badge"></span>
        {/if}
      </a>
      <a href="/specs" use:link class="nav-item" data-testid="nav-specs" class:active={$location === '/specs'}>
        <span class="nav-icon">📐</span>
        <span class="nav-label">Specs</span>
      </a>
      <a href="/experiments" use:link class="nav-item" data-testid="nav-experiments" class:active={$location === '/experiments'}>
        <span class="nav-icon">🔬</span>
        <span class="nav-label">Experiments</span>
      </a>
      <a href="/endpoints" use:link class="nav-item" data-testid="nav-endpoints" class:active={$location === '/endpoints'}>
        <span class="nav-icon">🔌</span>
        <span class="nav-label">Endpoints</span>
      </a>
      <a href="/playground" use:link class="nav-item" class:active={$location === '/playground'}>
        <span class="nav-icon">{$playgroundRunning ? '⚙️' : '🧪'}</span>
        <span class="nav-label">Playground</span>
        {#if $playgroundRunning}
          <span class="nav-badge"></span>
        {/if}
      </a>
    </nav>

    <RepoSelector />

    <div class="sidebar-panels">
      <RunnerPanel />
      <AgentPanel />
    </div>
  </aside>

  <main class="main">
    <Router {routes} />
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
    width: 320px;
    min-width: 280px;
    max-width: 360px;
    background: var(--surface-color);
    border-right: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    flex-shrink: 0;
  }

  @media (min-width: 1600px) {
    .sidebar {
      width: 340px;
    }
  }

  @media (max-width: 1200px) {
    .sidebar {
      width: 280px;
      min-width: 260px;
    }
  }

  @media (max-width: 768px) {
    .sidebar {
      width: 260px;
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

  .nav {
    display: flex;
    flex-direction: column;
    padding: 0.5rem;
    gap: 0.25rem;
    border-bottom: 1px solid var(--border-color);
    margin-bottom: 0.5rem;
  }

  .nav-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.95rem;
    font-weight: 500;
    position: relative;
  }

  .nav-item:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .nav-item.active {
    background: var(--selected-color);
    color: var(--primary-color);
  }

  .nav-icon {
    font-size: 1.1rem;
  }

  .nav-label {
    flex: 1;
  }

  .nav-badge {
    width: 8px;
    height: 8px;
    background: var(--warning-color);
    border-radius: 50%;
    animation: pulse 1.5s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  .sidebar-panels {
    flex: 1;
    overflow-y: auto;
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
