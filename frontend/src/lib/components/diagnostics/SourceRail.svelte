<script lang="ts">
  /**
   * The left rail: which sources are in the merged stream.
   *
   * A LIST OF TOGGLES, NOT A FILTER FORM. There is no Apply button and no
   * query builder - toggling a source re-renders immediately. Someone
   * debugging their own platform is narrowing by trial, and a form that makes
   * them commit to a guess before showing the result adds a round trip to
   * every guess.
   *
   * Toggling filters CLIENT-SIDE over the buffer the store already holds. It
   * deliberately does not re-fetch: a re-fetch would replace the entries
   * array wholesale, and the pane's selection-survival depends on that array
   * only ever being appended to. Changing the RUN does re-fetch, because a
   * different run is a different set of step logs - and that is an explicit
   * user action which resets the pane.
   */
  import type { LogSourceSummary, RunOption } from '../../stores/diagnostics';

  export let sources: LogSourceSummary[] = [];
  export let enabled: Set<string> = new Set();
  export let runs: RunOption[] = [];
  export let selectedRunId: string | null = null;
  export let onToggle: (id: string) => void;
  export let onSelectRun: (id: string | null) => void;

  function runLabel(run: RunOption): string {
    return `${run.has_failed_step ? '⚠ ' : ''}${run.label} · ${run.status}`;
  }
</script>

<aside class="rail" data-testid="log-source-rail">
  <div class="rail-section">
    <h3>Run</h3>
    <select
      data-testid="log-run-select"
      value={selectedRunId ?? ''}
      on:change={(e) => onSelectRun(e.currentTarget.value || null)}
    >
      <option value="">No run — backend and runner only</option>
      {#each runs as run}
        <option value={run.id}>{runLabel(run)}</option>
      {/each}
    </select>
    {#if runs.length === 0}
      <p class="rail-note">No pipeline runs on record.</p>
    {/if}
  </div>

  <div class="rail-section">
    <h3>Sources</h3>
    {#if sources.length === 0}
      <p class="rail-note">No sources reported.</p>
    {:else}
      <ul class="sources">
        {#each sources as source}
          <li>
            <label class="source" class:off={!enabled.has(source.id)}>
              <input
                type="checkbox"
                checked={enabled.has(source.id)}
                data-testid="source-toggle-{source.id}"
                on:change={() => onToggle(source.id)}
              />
              <span class="dot dot-{source.kind}"></span>
              <span class="label">{source.label}</span>
              {#if source.errors > 0}
                <span class="errors" title="{source.errors} errors">{source.errors}</span>
              {/if}
              <span class="count">{source.count}</span>
            </label>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</aside>

<style>
  .rail {
    width: 220px;
    flex-shrink: 0;
    border-right: 1px solid var(--border-color, #45475a);
    padding: 0.65rem 0.7rem;
    overflow-y: auto;
    background: var(--surface-color, #1e1e2e);
  }

  .rail-section + .rail-section {
    margin-top: 0.9rem;
  }

  h3 {
    margin: 0 0 0.35rem;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted, #6c7086);
  }

  select {
    width: 100%;
    padding: 0.3rem 0.4rem;
    background: var(--input-bg, #313244);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.75rem;
  }

  .rail-note {
    margin: 0.35rem 0 0;
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .sources {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .source {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.22rem 0.1rem;
    font-size: 0.76rem;
    color: var(--text-color, #cdd6f4);
    cursor: pointer;
    border-radius: 4px;
  }
  .source:hover {
    background: var(--hover-color, #313244);
  }
  .source.off {
    opacity: 0.5;
  }

  .source input {
    margin: 0;
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .dot-backend { background: #89b4fa; }
  .dot-step { background: #a6e3a1; }
  .dot-runner { background: #f9e2af; }
  .dot-browser { background: #cba6f7; }

  .label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .errors {
    font-size: 0.66rem;
    color: var(--error-color, #f38ba8);
    background: rgba(243, 139, 168, 0.15);
    border-radius: 3px;
    padding: 0 0.25rem;
  }

  .count {
    font-size: 0.68rem;
    color: var(--text-muted, #6c7086);
    font-variant-numeric: tabular-nums;
  }
</style>
