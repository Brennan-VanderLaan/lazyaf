<script lang="ts">
  import Board from '../components/Board.svelte';
  import { addRepoFormRequested, reposEverLoaded, reposLoadError } from '../components/RepoSelector.svelte';
  import { reposStore, selectedRepoId } from '../stores/repos';
  import { cardsStore } from '../stores/cards';

  const cardsLoading = cardsStore.loading;

  /** A cold install: the list really is empty, and we really did look. */
  $: firstRun = $reposEverLoaded && !$reposLoadError && $reposStore.length === 0;

  /**
   * R1: an unreachable backend must not be dressed up as an empty account.
   * Without this the board would cheerfully invite a newcomer to add their
   * first repository while the reason they see none is that nothing answered.
   *
   * Reads `reposLoadError`, NOT `reposStore.error` - the latter is the last
   * error from any repo operation, so a refused create would paint this over
   * the whole page while the add form was already explaining itself.
   */
  $: loadFailed = $reposEverLoaded && !!$reposLoadError && $reposStore.length === 0;

  /**
   * Which repo we have actually finished loading cards for.
   *
   * `cards.length === 0` is also true for the whole time a board is loading,
   * so testing it alone would flash "here is what a card is" across the
   * owner's populated board on every reload. Track the load per repo instead:
   * the hint may only appear about a board we have genuinely seen the inside
   * of.
   */
  let loadingSeenFor: string | null = null;
  let loadedFor: string | null = null;
  $: if ($cardsLoading) loadingSeenFor = $selectedRepoId;
  $: if (!$cardsLoading && loadingSeenFor === $selectedRepoId) loadedFor = loadingSeenFor;

  /**
   * A selected repo with a genuinely empty board. This is the moment a
   * newcomer has done everything right and still has to guess what a "card"
   * is - so it is the one moment worth spending words on. It disappears the
   * instant the first card exists, which is why it needs no dismiss button.
   */
  $: emptyBoard = !!$selectedRepoId && loadedFor === $selectedRepoId && $cardsStore.length === 0;

  function addFirstRepo() {
    addRepoFormRequested.set(true);
  }
</script>

<div class="board-page">
  {#if !$reposEverLoaded && !$selectedRepoId}
    <!--
      We do not know yet whether this account has repositories, and guessing
      wrong swaps the copy under the reader a moment after they start reading
      it. Hold the space instead of claiming something.
    -->
    <div class="board-page-waiting" aria-hidden="true"></div>
  {:else if loadFailed}
    <div class="no-repo" data-testid="repos-unavailable">
      <div class="first-run">
        <span class="first-run-icon">🔌</span>
        <h2>Could not load your repositories</h2>
        <p class="lede">{$reposLoadError}</p>
        <p class="note">
          This is a connection problem, not an empty account. The backend is
          not answering — check that it is running, then reload.
        </p>
      </div>
    </div>
  {:else if firstRun}
    <!--
      The cold-open screen. It used to read "No Repository Selected / Select a
      repository from the sidebar or add a new one to get started" and carry no
      control at all - on an install with zero repos, half of that sentence was
      impossible and the only actionable thing on screen was an unlabelled
      20px `+` in the sidebar chrome.

      The heading is deliberately unchanged: the owner knows this screen, and
      the confusing part was never the title. What is added is a control, and
      the three steps that were nowhere in the product.
    -->
    <div class="no-repo" data-testid="no-repo">
      <div class="first-run">
        <span class="first-run-icon">📁</span>
        <h2>No Repository Selected</h2>
        <p class="lede">
          LazyAF runs coding agents against your repositories. There are none
          yet — add one and you can start handing it work.
        </p>

        <button class="btn-primary" data-testid="first-run-add-repo" on:click={addFirstRepo}>
          Add your first repository
        </button>

        <ol class="steps" data-testid="first-run-steps">
          <li>
            <strong>Add a repository.</strong>
            LazyAF hosts its own copy of your code.
          </li>
          <li>
            <strong>Push your code to it.</strong>
            The exact <code>git remote add</code> and <code>git push</code>
            commands appear under <em>Repository Details</em> in the sidebar
            once the repository exists.
          </li>
          <li>
            <strong>Create a card.</strong>
            A card is one task you hand to an agent — a title and a
            description of what you want done. The agent works on its own
            branch and leaves the result in <em>In Review</em> for you.
          </li>
        </ol>

        <p class="note">
          Agents need a model to run: set <code>ANTHROPIC_API_KEY</code> in the
          backend's environment, or register a self-hosted one on the
          <em>Endpoints</em> page. Without a model, a submitted card fails.
        </p>
      </div>
    </div>
  {:else}
    {#if emptyBoard}
      <!--
        Four empty columns and a `+ New Card` button teach a stranger nothing
        about what a card is or who acts on it. One line, only while the board
        is genuinely empty.
      -->
      <aside class="board-hint" data-testid="empty-board-hint">
        <span class="board-hint-icon">💡</span>
        <p>
          A <strong>card</strong> is one task you hand to an agent. Write what
          you want done, then <strong>Create &amp; Submit</strong> — the agent
          works on its own branch and moves the card to <em>In Review</em>.
          If you have not pushed this repository yet, the
          <code>git push</code> command is under <em>Repository Details</em>
          in the sidebar.
        </p>
      </aside>
    {/if}
    <Board />
  {/if}
</div>

<style>
  .board-page {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .board-page-waiting {
    flex: 1;
  }

  .no-repo {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    overflow-y: auto;
  }

  .first-run {
    max-width: 34rem;
    text-align: left;
  }

  .first-run-icon {
    font-size: 3rem;
    display: block;
    margin-bottom: 0.75rem;
    opacity: 0.5;
  }

  .first-run h2 {
    margin: 0 0 0.5rem;
    color: var(--text-color, #cdd6f4);
    font-size: 1.5rem;
  }

  .lede {
    margin: 0 0 1.25rem;
    color: var(--text-muted, #6c7086);
    line-height: 1.55;
  }

  .btn-primary {
    padding: 0.6rem 1.1rem;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.95rem;
  }

  .btn-primary:hover {
    opacity: 0.9;
  }

  .steps {
    margin: 1.5rem 0 0;
    padding-left: 1.25rem;
    color: var(--text-muted, #6c7086);
    line-height: 1.55;
  }

  .steps li {
    margin-bottom: 0.6rem;
  }

  .steps strong {
    color: var(--text-color, #cdd6f4);
  }

  .steps em,
  .note em,
  .board-hint em {
    font-style: normal;
    color: var(--text-color, #cdd6f4);
  }

  .note {
    margin: 1.5rem 0 0;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color, #45475a);
    color: var(--text-muted, #6c7086);
    font-size: 0.85rem;
    line-height: 1.5;
  }

  code {
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    font-size: 0.85em;
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 3px;
    padding: 0.05rem 0.3rem;
    word-break: break-word;
  }

  .board-hint {
    display: flex;
    gap: 0.75rem;
    align-items: flex-start;
    margin: 1rem 1.5rem 0;
    padding: 0.75rem 1rem;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-left: 3px solid var(--primary-color, #89b4fa);
    border-radius: 6px;
    flex-shrink: 0;
  }

  .board-hint-icon {
    line-height: 1.55;
  }

  .board-hint p {
    margin: 0;
    color: var(--text-muted, #6c7086);
    font-size: 0.875rem;
    line-height: 1.55;
  }

  .board-hint strong {
    color: var(--text-color, #cdd6f4);
  }
</style>
