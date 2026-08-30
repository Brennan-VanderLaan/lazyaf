<script lang="ts">
  /**
   * Debug Re-run launcher (Phase 12.7).
   *
   * Layout is descended from the failure_01 modal (breakpoint checkbox list,
   * select-all/clear, commit radio pair) because that UX was the one part of
   * that attempt worth keeping. Everything under it is new:
   *
   *  - breakpoints are STEP KEYS, not indices (see stores/debug.ts
   *    `debugBreakpointOptions` - a v1 step's key is its index rendered as a
   *    string, a graph step's key is its stable step id);
   *  - the response carries NO token (`GET /api/debug/{id}` never serves
   *    one), so the handoff to the CLI is the join command;
   *  - a timeout can be chosen up front, because the pause is bounded by
   *    `expires_at` and the paused gate is the only thing that owns it.
   *
   * The modal deliberately does NOT warn about `on_pass: merge`: a debug
   * re-run rebuilds its trigger context from `branch`/`commit_sha` alone and
   * can neither merge a branch nor move a card. That is a backend guarantee
   * (C10), so restating it here as a caveat would be misinformation.
   */
  import { createEventDispatcher } from 'svelte';
  import type { Pipeline, PipelineRun, PipelineV2, DebugBreakpointOption } from '../../api/types';
  import { debugSessionsStore, debugBreakpointOptions } from '../../stores/debug';

  export let run: PipelineRun;
  export let pipeline: Pipeline | PipelineV2;

  const dispatch = createEventDispatcher<{
    close: void;
    started: { sessionId: string; runId: string; joinCommand: string };
  }>();

  /** Minutes offered for the pause budget; 240 is the backend's hard ceiling. */
  const TIMEOUT_CHOICES = [15, 30, 60, 120, 240];

  let options: DebugBreakpointOption[] = [];
  $: options = debugBreakpointOptions(pipeline);

  let selected = new Set<string>();
  let useOriginalCommit = true;
  let customBranch = '';
  let customCommitSha = '';
  let timeoutMinutes = 60;
  let submitting = false;
  let error: string | null = null;

  $: originalSha = run.trigger_context?.commit_sha ?? null;
  $: originalBranch = run.trigger_context?.branch ?? null;

  function toggle(key: string) {
    if (selected.has(key)) selected.delete(key);
    else selected.add(key);
    selected = selected; // Svelte reactivity on a mutated Set
  }

  function selectAll() {
    selected = new Set(options.map((o) => o.key));
  }

  function selectNone() {
    selected = new Set();
  }

  /** Options in list order, so the request mirrors what the user sees. */
  function selectedKeys(): string[] {
    return options.filter((o) => selected.has(o.key)).map((o) => o.key);
  }

  function stepTypeIcon(type: string): string {
    switch (type) {
      case 'script': return '>';
      case 'docker': return '#';
      case 'agent': return '@';
      case 'test': return '✓';
      default: return '?';
    }
  }

  async function start() {
    submitting = true;
    error = null;
    try {
      const response = await debugSessionsStore.startRerun(run.id, {
        breakpoints: selectedKeys(),
        use_original_commit: useOriginalCommit,
        commit_sha: useOriginalCommit ? null : customCommitSha.trim() || null,
        branch: useOriginalCommit ? null : customBranch.trim() || null,
        timeout_seconds: timeoutMinutes * 60,
      });
      dispatch('started', {
        sessionId: response.debug_session_id,
        runId: response.run_id,
        joinCommand: response.join_command,
      });
    } catch (e) {
      // Surfaced in place: an unknown step key is a 400 whose detail names the
      // key, and swallowing it would leave the user staring at a modal that
      // did nothing.
      error = e instanceof Error ? e.message : 'Failed to start debug re-run';
    } finally {
      submitting = false;
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') dispatch('close');
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
  class="modal-backdrop"
  on:click={() => dispatch('close')}
  role="dialog"
  aria-modal="true"
  aria-label="Debug re-run"
  tabindex="-1"
>
  <div class="modal" data-testid="debug-rerun-modal" on:click|stopPropagation role="document">
    <header class="modal-header">
      <h2>Debug Re-run</h2>
      <button type="button" class="close-btn" on:click={() => dispatch('close')} aria-label="Close">✕</button>
    </header>

    <div class="modal-body">
      <p class="description">
        Re-runs this pipeline and pauses <em>before</em> each selected step. While paused you can
        attach a shell to the run's workspace, change files, then resume.
      </p>

      <section class="section">
        <div class="section-header">
          <h3>Breakpoints</h3>
          <div class="section-actions">
            <button type="button" class="link-btn" data-testid="breakpoints-select-all" on:click={selectAll}>Select all</button>
            <span class="separator">|</span>
            <button type="button" class="link-btn" data-testid="breakpoints-clear" on:click={selectNone}>Clear</button>
          </div>
        </div>

        <div class="steps-list" data-testid="breakpoint-list">
          {#each options as option (option.key)}
            <label
              class="step-item"
              data-testid="breakpoint-item"
              data-step-key={option.key}
              class:selected={selected.has(option.key)}
            >
              <input
                type="checkbox"
                checked={selected.has(option.key)}
                on:change={() => toggle(option.key)}
              />
              <span class="step-icon">{stepTypeIcon(option.type)}</span>
              <span class="step-name">{option.name}</span>
              <span class="step-type">{option.type}</span>
            </label>
          {/each}
          {#if options.length === 0}
            <p class="hint">This pipeline has no steps to break on.</p>
          {/if}
        </div>

        <p class="hint" data-testid="breakpoint-hint">
          {#if selected.size === 0}
            No breakpoints selected — the re-run will execute straight through.
          {:else}
            {selected.size} breakpoint{selected.size === 1 ? '' : 's'} selected.
          {/if}
        </p>
      </section>

      <section class="section">
        <h3>Commit</h3>
        <div class="commit-options">
          <label class="radio-option">
            <input type="radio" bind:group={useOriginalCommit} value={true} />
            <span>Same as this run</span>
            {#if originalSha}
              <code data-testid="original-commit">{originalSha.substring(0, 8)}</code>
            {:else if originalBranch}
              <code data-testid="original-commit">{originalBranch}</code>
            {/if}
          </label>
          <label class="radio-option">
            <input type="radio" bind:group={useOriginalCommit} value={false} />
            <span>Different branch or commit</span>
          </label>
        </div>

        {#if !useOriginalCommit}
          <div class="custom-commit" data-testid="custom-commit">
            <input type="text" placeholder="Branch name (optional)" bind:value={customBranch} />
            <input type="text" placeholder="Commit SHA (optional)" bind:value={customCommitSha} />
          </div>
        {/if}
      </section>

      <section class="section">
        <h3>Pause budget</h3>
        <div class="timeout-row">
          <select bind:value={timeoutMinutes} data-testid="debug-timeout">
            {#each TIMEOUT_CHOICES as minutes}
              <option value={minutes}>{minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`}</option>
            {/each}
          </select>
          <span class="hint">
            How long a breakpoint may hold the run before it times out. Extendable while paused.
          </span>
        </div>
      </section>

      {#if error}
        <div class="error" data-testid="debug-rerun-error">{error}</div>
      {/if}
    </div>

    <footer class="modal-footer">
      <button type="button" class="btn-secondary" on:click={() => dispatch('close')} disabled={submitting}>
        Cancel
      </button>
      <button
        type="button"
        class="btn-primary"
        data-testid="start-debug-btn"
        on:click={start}
        disabled={submitting || options.length === 0}
      >
        {submitting ? 'Starting…' : 'Start Debug Re-run'}
      </button>
    </footer>
  </div>
</div>

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1100;
    padding: 2rem;
  }

  .modal {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    width: 100%;
    max-width: 560px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.25rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.1rem;
  }

  .close-btn {
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 1.1rem;
    cursor: pointer;
    padding: 0.25rem 0.5rem;
  }

  .close-btn:hover {
    color: var(--text-color);
  }

  .modal-body {
    padding: 1.25rem 1.5rem;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .description {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.9rem;
    line-height: 1.5;
  }

  .section h3 {
    margin: 0 0 0.5rem;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }

  .section-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .link-btn {
    background: none;
    border: none;
    color: var(--primary-color, #4a9eff);
    cursor: pointer;
    font-size: 0.8rem;
    padding: 0;
  }

  .separator {
    color: var(--text-muted);
    font-size: 0.8rem;
  }

  .steps-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.5rem;
    max-height: 260px;
    overflow-y: auto;
  }

  .step-item {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.4rem 0.5rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.9rem;
  }

  .step-item:hover {
    background: var(--hover-color, rgba(255, 255, 255, 0.05));
  }

  .step-item.selected {
    background: var(--hover-color, rgba(255, 255, 255, 0.08));
  }

  .step-icon {
    font-family: monospace;
    color: var(--text-muted);
    width: 1rem;
    text-align: center;
  }

  .step-name {
    flex: 1;
  }

  .step-type {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .hint {
    margin: 0.5rem 0 0;
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .commit-options {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .radio-option {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .radio-option code {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .custom-commit {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.6rem;
  }

  .custom-commit input {
    flex: 1;
    padding: 0.45rem 0.6rem;
    background: var(--bg-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.85rem;
  }

  .timeout-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .timeout-row select {
    padding: 0.4rem 0.6rem;
    background: var(--bg-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
  }

  .timeout-row .hint {
    margin: 0;
    flex: 1;
    min-width: 12rem;
  }

  .error {
    padding: 0.6rem 0.75rem;
    border-radius: 6px;
    background: rgba(255, 80, 80, 0.12);
    border: 1px solid var(--error-color);
    color: var(--error-color);
    font-size: 0.85rem;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
    padding: 1rem 1.5rem;
    border-top: 1px solid var(--border-color);
  }

  .btn-primary,
  .btn-secondary {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-size: 0.9rem;
    cursor: pointer;
    border: 1px solid var(--border-color);
  }

  .btn-primary {
    background: var(--primary-color, #4a9eff);
    border-color: var(--primary-color, #4a9eff);
    color: #fff;
  }

  .btn-secondary {
    background: transparent;
    color: var(--text-color);
  }

  .btn-primary:disabled,
  .btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
