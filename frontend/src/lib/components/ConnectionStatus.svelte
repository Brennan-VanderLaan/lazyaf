<script lang="ts">
  /**
   * ConnectionStatus — the answer to "is what I am looking at real?"
   *
   * QA triage T7: `websocketStore.status` was a fully-computed four-state
   * store with ZERO consumers. With the backend stopped the board kept
   * rendering its last known cards as though live: no banner, no badge, no
   * toast. A demo audience cannot tell a frozen pipeline from a dead server,
   * and neither could the operator.
   *
   * Two surfaces, deliberately:
   *
   *   - an always-present inline chip in the sidebar, so "live" is a claim the
   *     UI makes explicitly and continuously rather than by omission;
   *   - a fixed banner while the socket is down, because the sidebar scrolls
   *     and an outage must not be something you have to go looking for.
   *
   * Reconnect is automatic (3s). "Try now" exists because waiting out a timer
   * you cannot see is indistinguishable from a hang.
   *
   * MOUNT POINT: rendered from RepoSelector, which is the topmost always-mounted
   * component inside `src/lib/`. Its natural home is App.svelte's sidebar,
   * above <RepoSelector /> — see the report's requested-edit list.
   */
  import { onDestroy } from 'svelte';
  import { websocketStore } from '../stores/websocket';
  import { formatAge } from '../utils/time';

  const status = websocketStore.status;
  const lastSyncedAt = websocketStore.lastSyncedAt;
  const reconnectAttempts = websocketStore.reconnectAttempts;
  const resyncing = websocketStore.resyncing;

  /** Ticks so "last synced 40s ago" keeps counting while the socket is down. */
  let now = Date.now();
  const tick = setInterval(() => (now = Date.now()), 1000);
  onDestroy(() => clearInterval(tick));

  $: offline = $status === 'disconnected' || $status === 'error';

  $: label =
    $resyncing
      ? 'Resyncing…'
      : $status === 'connected'
        ? 'Live'
        : $status === 'connecting'
          ? 'Connecting…'
          : $status === 'error'
            ? 'Connection error'
            : 'Offline';

  /**
   * What the chip's title/aria says. Never claims freshness it cannot back:
   * while offline it reports how old the data actually is.
   */
  $: detail = offline
    ? $lastSyncedAt
      ? `Backend unreachable. Showing data from ${formatAge($lastSyncedAt, now)} ago.`
      : 'Backend unreachable. No data has loaded yet.'
    : $status === 'connecting'
      ? 'Opening the live update socket…'
      : 'Live updates are flowing from the backend.';
</script>

<div
  class="connection-status"
  class:offline
  class:connecting={$status === 'connecting'}
  class:connected={$status === 'connected'}
  data-testid="connection-status"
  data-status={$status}
  role="status"
  aria-live="polite"
  title={detail}
>
  <span class="dot" aria-hidden="true"></span>
  <span class="label">{label}</span>
  {#if offline && $reconnectAttempts > 0}
    <span class="attempts" data-testid="connection-attempts">retry {$reconnectAttempts}</span>
  {/if}
</div>

{#if offline}
  <!--
    The board itself does not change when the socket dies, so without this the
    only evidence of an outage is that nothing ever moves again.
  -->
  <div class="connection-banner" data-testid="connection-banner" role="alert">
    <span class="banner-icon" aria-hidden="true">⚠</span>
    <span class="banner-text">
      Backend unreachable — this board is
      {#if $lastSyncedAt}
        <strong>{formatAge($lastSyncedAt, now)} stale</strong>
      {:else}
        <strong>not loaded</strong>
      {/if}
      and will not update. Reconnecting automatically…
    </span>
    <button
      class="banner-retry"
      data-testid="connection-retry"
      on:click={() => websocketStore.retryNow()}
    >
      Try now
    </button>
  </div>
{/if}

<style>
  .connection-status {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.35rem 0.75rem;
    font-size: 0.72rem;
    letter-spacing: 0.3px;
    color: var(--text-muted, #6c7086);
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--text-muted, #6c7086);
    flex-shrink: 0;
  }

  .connected .dot {
    background: var(--success-color, #a6e3a1);
  }

  .connecting .dot {
    background: var(--warning-color, #f9e2af);
    animation: blink 1.2s infinite;
  }

  .offline {
    color: var(--error-color, #f38ba8);
  }

  .offline .dot {
    background: var(--error-color, #f38ba8);
    animation: blink 1.2s infinite;
  }

  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }

  .label {
    flex: 1;
  }

  .attempts {
    font-family: var(--font-mono, monospace);
    font-size: 0.65rem;
    opacity: 0.8;
  }

  /* Fixed, because the sidebar scrolls and an outage must not scroll away. */
  .connection-banner {
    position: fixed;
    top: 0;
    left: 50%;
    transform: translateX(-50%);
    z-index: 1000;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    max-width: min(720px, 92vw);
    padding: 0.5rem 0.9rem;
    border: 1px solid var(--error-color, #f38ba8);
    border-top: none;
    border-radius: 0 0 8px 8px;
    background: var(--surface-color, #1e1e2e);
    color: var(--text-color, #cdd6f4);
    font-size: 0.8rem;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
  }

  .banner-icon {
    color: var(--error-color, #f38ba8);
    font-size: 1rem;
    flex-shrink: 0;
  }

  .banner-text {
    flex: 1;
    min-width: 0;
  }

  .banner-retry {
    flex-shrink: 0;
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 5px;
    background: var(--surface-alt, #181825);
    color: var(--text-color, #cdd6f4);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .banner-retry:hover {
    background: var(--hover-color, #313244);
  }
</style>
