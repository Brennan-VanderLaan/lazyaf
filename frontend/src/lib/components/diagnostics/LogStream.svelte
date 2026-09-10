<script lang="ts">
  /**
   * The merged log pane.
   *
   * THE SCROLL CONTRACT IS PORTED FROM PlaygroundPage, NOT REINVENTED.
   * ------------------------------------------------------------------
   * Three behaviours here cost a full adversarial pass to find on the
   * playground and are trivial to undo by accident. They are reproduced
   * deliberately, with the same names, so the two panes can be diffed:
   *
   *  1. FOLLOW MODE IS A LATCH, NOT A DEBOUNCE. The original playground code
   *     armed a 100ms debounced snap on every incoming line and re-checked
   *     only that the container still existed when it fired. A snap armed by
   *     the last line before the user scrolled up still yanked them back, and
   *     because every new line RE-ARMED the timer it could only ever fire
   *     during a pause - which is exactly what a real agent does constantly.
   *
   *  2. OUR OWN SCROLLS ARE IGNORED BY THE SCROLL HANDLER, via a TIMESTAMP
   *     window rather than a boolean flag. A flag set around the assignment
   *     has to be cleared by the scroll event it expects; a browser that
   *     coalesces that event away - or a hidden tab, where it never fires -
   *     leaves the flag armed to swallow the user's next real scroll. A
   *     window expires by itself. Without this, the snap lands at the bottom,
   *     the handler reads "near the bottom" and re-arms following, and the
   *     user is put straight back on the leash they just escaped.
   *
   *  3. A LIVE SELECTION SUPPRESSES FOLLOWING. Scrolling content under a held
   *     mouse button is what blew a drag-selection up from 73 characters to
   *     1781: the browser correctly extends a live selection to wherever the
   *     cursor now points. `selecting` covers the drag; `paneHasSelection()`
   *     covers the standing selection afterwards, because following the tail
   *     past text someone highlighted IN ORDER TO COPY IT is how a transcript
   *     gets scrolled out from under them. The release that ends a drag often
   *     happens OUTSIDE the pane, so the listener is on the window - a
   *     pane-local `pointerup` would never fire and following would stay
   *     suppressed forever.
   *
   * The each-block is UNKEYED for the same reason it is unkeyed there, and
   * that is also why this pane's buffer APPENDS instead of being replaced by
   * a re-fetched array. See `entries` below.
   */
  import { tick } from 'svelte';
  import { splitRedactions, diagnosticsStore, type LogEntry } from '../../stores/diagnostics';
  import { parseTimestamp, UNKNOWN } from '../../utils/time';

  /**
   * APPEND-ONLY. The store fetches with a cursor and pushes onto the tail;
   * the parent filters it into this prop.
   *
   * Keying the each-block on this array would replace every node on every
   * flush and CREATE the selection bug the playground was reported for.
   * Unkeyed, Svelte index-reconciles: identical leading entries produce no
   * DOM writes at all, so a selection over earlier lines survives new output
   * arriving. This is why the store must never insert into the middle of the
   * buffer, and why front-eviction is deferred while the user is reading.
   */
  export let entries: LogEntry[] = [];
  export let dropped = 0;
  export let live = true;
  export let errorsOnly = false;
  export let emptyMessage = 'No log records for the selected sources.';

  let logsContainer: HTMLDivElement;

  /** True while the pane should stay pinned to the newest output. */
  let followTail = true;
  /** When we last scrolled the pane ourselves. A timestamp, deliberately. */
  let programmaticScrollAt = 0;
  const PROGRAMMATIC_SCROLL_WINDOW_MS = 150;
  /** True between pointerdown in the pane and the matching release. */
  let selecting = false;
  /** Distance from the bottom, in px, still counted as "at the bottom". */
  const FOLLOW_THRESHOLD_PX = 24;

  $: if (entries && logsContainer) {
    void keepPinned();
  }

  /**
   * The store may only evict from the front of the buffer while the user is
   * parked at the tail with nothing selected. Front-eviction shifts every
   * index in an unkeyed each-block, which rewrites every visible line - it
   * collapses a standing selection and moves the text under the reader. So
   * the lease follows follow-mode exactly.
   */
  $: diagnosticsStore.holdTrim(!followTail || selecting);

  async function keepPinned() {
    await tick();
    if (!logsContainer || !followTail || selecting || paneHasSelection()) return;
    scrollToBottom();
  }

  /** True while the user has text highlighted inside this pane. */
  function paneHasSelection(): boolean {
    if (!logsContainer) return false;
    const selection = document.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
    return logsContainer.contains(selection.getRangeAt(0).commonAncestorContainer);
  }

  function scrollToBottom() {
    if (!logsContainer) return;
    const target = logsContainer.scrollHeight - logsContainer.clientHeight;
    if (Math.abs(logsContainer.scrollTop - target) < 1) return;
    programmaticScrollAt = Date.now();
    logsContainer.scrollTop = logsContainer.scrollHeight;
  }

  function handleScroll() {
    if (!logsContainer) return;
    if (Date.now() - programmaticScrollAt < PROGRAMMATIC_SCROLL_WINDOW_MS) return;
    const { scrollTop, scrollHeight, clientHeight } = logsContainer;
    followTail = scrollHeight - scrollTop - clientHeight <= FOLLOW_THRESHOLD_PX;
  }

  function handlePointerDown() {
    // A press in the pane is the start of a selection until proven otherwise.
    selecting = true;
  }

  function handleWindowPointerUp() {
    if (!selecting) return;
    selecting = false;
    if (followTail) void keepPinned();
  }

  export function jumpToNewest() {
    followTail = true;
    void keepPinned();
  }

  function clockTime(ts: string): string {
    const at = parseTimestamp(ts);
    if (at === null) return UNKNOWN;
    const d = new Date(at);
    const pad = (n: number, width = 2) => String(n).padStart(width, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }

  function levelClass(level: string): string {
    const upper = (level || '').toUpperCase();
    if (upper === 'ERROR' || upper === 'CRITICAL' || upper === 'FATAL') return 'lvl-error';
    if (upper === 'WARNING' || upper === 'WARN') return 'lvl-warn';
    if (upper === 'DEBUG') return 'lvl-debug';
    return 'lvl-info';
  }
</script>

<!--
  The release that ends a drag-selection often happens OUTSIDE the pane, so
  the pane's own pointerup would never fire and autoscroll would stay
  suppressed forever.
-->
<svelte:window on:pointerup={handleWindowPointerUp} on:pointercancel={handleWindowPointerUp} />

<div class="stream-wrap">
  {#if dropped > 0}
    <!-- R1: lines that were dropped are stated, never silently missing. -->
    <div class="dropped-note" data-testid="log-dropped">
      {dropped.toLocaleString()} older lines dropped from this view to bound memory.
    </div>
  {/if}

  <div
    class="stream"
    data-testid="log-stream"
    bind:this={logsContainer}
    on:scroll={handleScroll}
    on:pointerdown={handlePointerDown}
  >
    {#if entries.length === 0}
      <div class="stream-empty" data-testid="log-stream-empty">
        {#if errorsOnly}
          <span>No errors in the selected sources. Untick "errors only" to see everything.</span>
        {:else if live}
          <span class="spinner"></span>
          <span>{emptyMessage}</span>
        {:else}
          <span>{emptyMessage}</span>
        {/if}
      </div>
    {:else}
      <!--
        NOT KEYED, deliberately. This block only ever APPENDS, and Svelte
        index-reconciles it without touching existing text nodes - so a
        selection over earlier lines survives new output arriving. Keying it
        would replace nodes on every flush and CREATE the selection bug.
      -->
      {#each entries as entry}
        <div class="log-line {levelClass(entry.level)}" data-source-kind={entry.kind}>
          <span class="ts">{clockTime(entry.ts)}</span>
          <span class="src src-{entry.kind}">{entry.source}</span>
          <span class="lvl">{entry.level}</span>
          <span class="msg"
            >{#each splitRedactions(entry.message) as segment}{#if segment.redacted}<span
                  class="redacted"
                  data-testid="redacted-span"
                  title="A credential-shaped value was removed here. The digest is salted per bundle and carries none of the secret."
                  >{segment.text}</span
                >{:else}{segment.text}{/if}{/each}</span
          >
        </div>
      {/each}
    {/if}
  </div>

  {#if !followTail && entries.length > 0}
    <button class="follow-resume" data-testid="log-follow-resume" on:click={jumpToNewest}>
      Jump to newest output
    </button>
  {/if}
</div>

<style>
  .stream-wrap {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }

  .dropped-note {
    padding: 0.35rem 0.75rem;
    font-size: 0.75rem;
    color: var(--text-muted, #a6adc8);
    background: var(--surface-alt, #181825);
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .stream {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overflow-x: auto;
    padding: 0.5rem 0.75rem;
    background: var(--bg-color, #11111b);
    font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
    font-size: 0.78rem;
    line-height: 1.55;
  }

  .stream-empty {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 1.5rem 0.25rem;
    color: var(--text-muted, #a6adc8);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 0.85rem;
  }

  .log-line {
    display: grid;
    grid-template-columns: 5.5rem 7rem 4.25rem 1fr;
    gap: 0.6rem;
    white-space: pre-wrap;
    word-break: break-word;
    padding: 0.05rem 0;
  }

  .ts {
    color: var(--text-muted, #6c7086);
    white-space: nowrap;
  }

  .src {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-weight: 600;
  }
  .src-backend { color: #89b4fa; }
  .src-step { color: #a6e3a1; }
  .src-runner { color: #f9e2af; }
  .src-browser { color: #cba6f7; }

  .lvl {
    color: var(--text-muted, #6c7086);
    white-space: nowrap;
  }

  .msg {
    color: var(--text-color, #cdd6f4);
  }

  .lvl-error .lvl,
  .lvl-error .msg {
    color: var(--error-color, #f38ba8);
  }
  .lvl-warn .lvl,
  .lvl-warn .msg {
    color: var(--warning-color, #f9e2af);
  }
  .lvl-debug .msg {
    color: var(--text-muted, #a6adc8);
  }

  /*
    Redaction is highlighted rather than hidden: it is the only on-screen
    evidence that the safety net ran. A bundle that silently failed to redact
    and one that had nothing to redact look identical without this.
  */
  .redacted {
    background: rgba(203, 166, 247, 0.18);
    border: 1px solid rgba(203, 166, 247, 0.45);
    border-radius: 3px;
    padding: 0 0.2rem;
    color: #cba6f7;
    font-weight: 600;
    cursor: help;
  }

  .follow-resume {
    position: absolute;
    right: 1rem;
    bottom: 1rem;
    padding: 0.4rem 0.8rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #11111b);
    border: none;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.45);
  }

  .follow-resume:hover {
    filter: brightness(1.1);
  }

  .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid var(--border-color, #45475a);
    border-top-color: var(--primary-color, #89b4fa);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: inline-block;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
