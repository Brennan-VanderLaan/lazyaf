<script lang="ts">
  /**
   * The experiment launch form.
   *
   * The whole point of this component is the ORDER of its two buttons: the
   * matrix can only be launched after it has been costed, and any edit to a
   * cost-bearing field invalidates the estimate and re-disables Launch. The
   * gate itself lives in the store (`estimateIsFresh`) so it is unit-tested;
   * this component only reflects it.
   */
  import { onMount, createEventDispatcher } from 'svelte';
  import type {
    Card,
    UserStory,
    PromptTemplate,
    Repo,
    ExperimentCreate,
    ExperimentModelAxis,
    ExperimentPromptAxis,
    ExperimentTargetType,
  } from '../../api/types';
  import {
    cards as cardsApi,
    userStories as storiesApi,
    promptTemplates as templatesApi,
    features as featuresApi,
  } from '../../api/client';
  import { reposStore } from '../../stores/repos';
  import { experimentsStore, estimateIsFresh, cellCount } from '../../stores/experiments';
  import { EndpointSelect } from '../endpoint';

  const dispatch = createEventDispatcher<{ launched: { id: string } }>();

  /**
   * The agent vocabulary, mirroring agent_run.AGENT_BY_RUNNER_TYPE's VALUES.
   * There is no model-name -> agent inference anywhere in this flow: an
   * unknown agent is a 422 from the backend, not a guess (R1).
   */
  const AGENTS = ['claude-code', 'gemini', 'mock', 'openai-harness'];

  /**
   * M14: THIS is what lets one matrix mix API and self-hosted models in a
   * single run - the whole point of the milestone.
   *
   * `MatrixModelEntry` needs no schema change to do it: a self-hosted cell is
   * just `{agent: "openai-harness", model: "endpoint:local-4090"}`, because
   * `model` is already the field the resolver reads and `endpoint:<name>` is
   * already the one sugar spelling it parses. The row's free-text model input
   * is swapped for an endpoint picker so the operator cannot typo a
   * coordinate the leaderboard will then key history on.
   */
  function isHarnessRow(agent: string): boolean {
    return agent === 'openai-harness';
  }

  let name = '';
  let description = '';
  let targetType: ExperimentTargetType = 'card';
  let repoId = '';
  let targetId = '';
  let repeat = 3;
  let budgetUsd = '5.00';
  let maxConcurrency = 2;
  let pushBranches = false;
  let verifyEnabled = false;
  let verifyImage = 'python:3.12';
  let verifyCommand = 'pytest -p runner_common.pytest_lazyaf';
  let verifyTimeout = 900;

  let modelRows: ExperimentModelAxis[] = [{ agent: 'mock', model: '', label: '' }];
  let promptRows: ExperimentPromptAxis[] = [{ prompt_template_id: null, label: '' }];

  let cardOptions: Card[] = [];
  let storyOptions: UserStory[] = [];
  let templates: PromptTemplate[] = [];
  let storyRepoIds: Record<string, string[]> = {};
  let loadingTargets = false;

  onMount(async () => {
    await reposStore.load();
    try {
      const [templateList, stories, features] = await Promise.all([
        templatesApi.list(),
        storiesApi.list(),
        featuresApi.list(),
      ]);
      templates = templateList;
      storyOptions = stories;
      // A story spans a feature's repos; the backend refuses to guess which
      // one, so the form asks whenever there is more than one.
      const byFeature: Record<string, string[]> = {};
      for (const feature of features) byFeature[feature.id] = feature.repo_ids ?? [];
      for (const story of stories) storyRepoIds[story.id] = byFeature[story.feature_id] ?? [];
    } catch {
      // Target pickers stay empty; the launch itself still validates server-side.
    }
  });

  async function loadCards(id: string) {
    if (!id) {
      cardOptions = [];
      return;
    }
    loadingTargets = true;
    try {
      cardOptions = await cardsApi.list(id);
    } catch {
      cardOptions = [];
    } finally {
      loadingTargets = false;
    }
  }

  $: repos = $reposStore as Repo[];
  $: if (targetType === 'card') void loadCards(repoId);

  /** A user-story target carries its own repo set; keep repoId honest. */
  $: storyRepos = targetType === 'user_story' && targetId ? storyRepoIds[targetId] ?? [] : [];

  $: draft = {
    name: name.trim(),
    description: description.trim(),
    target_type: targetType,
    target_id: targetId,
    repo_id: repoId,
    matrix: {
      models: modelRows.map(row => ({
        agent: row.agent,
        // "" in the input means "the CLI's own default", which the wire spells
        // null. An empty string would be a model literally named "".
        model: row.model ? row.model : null,
        label: row.label ? row.label : null,
      })),
      prompts: promptRows.map(row => ({
        prompt_template_id: row.prompt_template_id ? row.prompt_template_id : null,
        label: row.label ? row.label : null,
      })),
      repeat,
    },
    verify: verifyEnabled
      ? { image: verifyImage, command: verifyCommand, timeout: verifyTimeout }
      : null,
    budget_usd: budgetUsd,
    max_concurrency: maxConcurrency,
    push_branches: pushBranches,
  } satisfies ExperimentCreate;

  $: cells = cellCount(draft);
  $: estimateFresh = estimateIsFresh($experimentsStore, draft);
  $: formComplete =
    draft.name.length > 0 &&
    targetId.length > 0 &&
    repoId.length > 0 &&
    modelRows.length > 0 &&
    promptRows.length > 0 &&
    repeat >= 1 &&
    budgetUsd.trim().length > 0;

  /**
   * Any edit to a priced field drops the estimate, which re-disables Launch.
   * Bound to the inputs rather than inferred from a watcher so the invalidation
   * is impossible to miss when a field is added.
   */
  function invalidateEstimate() {
    if ($experimentsStore.estimate !== null) experimentsStore.clearEstimate();
  }

  function addModelRow() {
    modelRows = [...modelRows, { agent: 'mock', model: '', label: '' }];
    invalidateEstimate();
  }

  function removeModelRow(index: number) {
    modelRows = modelRows.filter((_, i) => i !== index);
    invalidateEstimate();
  }

  function addPromptRow() {
    promptRows = [...promptRows, { prompt_template_id: null, label: '' }];
    invalidateEstimate();
  }

  function removePromptRow(index: number) {
    promptRows = promptRows.filter((_, i) => i !== index);
    invalidateEstimate();
  }

  async function runDryRun() {
    await experimentsStore.dryRun(draft);
  }

  async function launch() {
    const id = await experimentsStore.createAndLaunch(draft);
    if (id) dispatch('launched', { id });
  }
</script>

<div class="matrix-builder" data-testid="matrix-builder">
  <div class="field-grid">
    <label class="field">
      <span>Name</span>
      <input data-testid="experiment-name-input" type="text" bind:value={name} placeholder="opus vs haiku" />
    </label>

    <label class="field">
      <span>Repo</span>
      <select data-testid="experiment-repo-select" bind:value={repoId} on:change={invalidateEstimate}>
        <option value="">Select a repo...</option>
        {#each repos as repo (repo.id)}
          <option value={repo.id}>{repo.name}</option>
        {/each}
      </select>
    </label>

    <label class="field">
      <span>Target</span>
      <select
        data-testid="experiment-target-type-select"
        bind:value={targetType}
        on:change={() => { targetId = ''; invalidateEstimate(); }}
      >
        <option value="card">Card</option>
        <option value="user_story">User story</option>
      </select>
    </label>

    <label class="field">
      <span>{targetType === 'card' ? 'Card' : 'User story'}</span>
      {#if targetType === 'card'}
        <select data-testid="experiment-target-select" bind:value={targetId} on:change={invalidateEstimate}>
          <option value="">{loadingTargets ? 'Loading...' : 'Select a card...'}</option>
          {#each cardOptions as card (card.id)}
            <option value={card.id}>{card.title}</option>
          {/each}
        </select>
      {:else}
        <select data-testid="experiment-target-select" bind:value={targetId} on:change={invalidateEstimate}>
          <option value="">Select a user story...</option>
          {#each storyOptions as story (story.id)}
            <option value={story.id}>{story.title}</option>
          {/each}
        </select>
      {/if}
    </label>
  </div>

  {#if targetType === 'user_story' && storyRepos.length > 1}
    <p class="hint" data-testid="story-repo-hint">
      This story spans {storyRepos.length} repos — pick the one the experiment runs in above.
      The backend refuses to guess.
    </p>
  {/if}

  <!-- Model axis -->
  <section class="axis">
    <header>
      <h3>Models</h3>
      <button type="button" class="btn-small" data-testid="add-model-row-btn" on:click={addModelRow}>
        + model
      </button>
    </header>
    {#each modelRows as row, i (i)}
      <div class="axis-row" data-testid="matrix-model-row">
        <select
          data-testid="model-agent-select"
          bind:value={row.agent}
          on:change={invalidateEstimate}
        >
          {#each AGENTS as agent (agent)}
            <option value={agent}>{agent}</option>
          {/each}
        </select>
        {#if isHarnessRow(row.agent)}
          <EndpointSelect
            testid="model-endpoint-select"
            value={row.model ?? ''}
            onChange={(value) => {
              row.model = value;
              modelRows = modelRows;
              invalidateEstimate();
            }}
          />
        {:else}
          <input
            data-testid="model-name-input"
            type="text"
            placeholder="model id (blank = CLI default)"
            bind:value={row.model}
            on:input={invalidateEstimate}
          />
        {/if}
        <input
          data-testid="model-label-input"
          type="text"
          placeholder="label"
          bind:value={row.label}
        />
        <button
          type="button"
          class="btn-icon"
          data-testid="remove-model-row-btn"
          disabled={modelRows.length <= 1}
          on:click={() => removeModelRow(i)}
        >
          ×
        </button>
      </div>
    {/each}
  </section>

  <!-- Prompt axis -->
  <section class="axis">
    <header>
      <h3>Prompts</h3>
      <button type="button" class="btn-small" data-testid="add-prompt-row-btn" on:click={addPromptRow}>
        + prompt
      </button>
    </header>
    {#each promptRows as row, i (i)}
      <div class="axis-row" data-testid="matrix-prompt-row">
        <select
          data-testid="prompt-template-select"
          bind:value={row.prompt_template_id}
          on:change={invalidateEstimate}
        >
          <option value={null}>platform default prompt</option>
          {#each templates as template (template.id)}
            <option value={template.id}>{template.name}</option>
          {/each}
        </select>
        <input
          data-testid="prompt-label-input"
          type="text"
          placeholder="label"
          bind:value={row.label}
        />
        <button
          type="button"
          class="btn-icon"
          data-testid="remove-prompt-row-btn"
          disabled={promptRows.length <= 1}
          on:click={() => removePromptRow(i)}
        >
          ×
        </button>
      </div>
    {/each}
  </section>

  <div class="field-grid">
    <label class="field">
      <span>Repeat</span>
      <input
        data-testid="repeat-input"
        type="number"
        min="1"
        bind:value={repeat}
        on:input={invalidateEstimate}
      />
    </label>

    <label class="field">
      <span>Budget (USD)</span>
      <input
        data-testid="budget-input"
        type="text"
        inputmode="decimal"
        bind:value={budgetUsd}
        on:input={invalidateEstimate}
      />
    </label>

    <label class="field">
      <span>Max concurrency</span>
      <input data-testid="concurrency-input" type="number" min="1" max="8" bind:value={maxConcurrency} />
    </label>

    <div class="field">
      <span>Cells</span>
      <output class="cells-readout" data-testid="matrix-cell-count">{cells}</output>
    </div>
  </div>

  <!-- Verify step: without it, the only test evidence is whatever the agent shipped -->
  <section class="verify">
    <label class="toggle">
      <input
        data-testid="verify-toggle"
        type="checkbox"
        bind:checked={verifyEnabled}
        on:change={invalidateEstimate}
      />
      <span>Run a verify step in every cell (this is where TestRun evidence comes from)</span>
    </label>
    {#if verifyEnabled}
      <div class="field-grid">
        <label class="field">
          <span>Image</span>
          <input data-testid="verify-image-input" type="text" bind:value={verifyImage} />
        </label>
        <label class="field">
          <span>Command</span>
          <input data-testid="verify-command-input" type="text" bind:value={verifyCommand} />
        </label>
        <label class="field">
          <span>Timeout (s)</span>
          <input data-testid="verify-timeout-input" type="number" min="1" bind:value={verifyTimeout} />
        </label>
      </div>
    {:else}
      <p class="hint">
        Without a verify step a cell has no test evidence, so its pass rate reads
        <strong>N/A</strong> — not 0%.
      </p>
    {/if}
  </section>

  <label class="toggle">
    <input
      data-testid="push-branches-toggle"
      type="checkbox"
      bind:checked={pushBranches}
      on:change={invalidateEstimate}
    />
    <span>
      Push each cell's branch (keeps the diffs). A push-triggered pipeline with no
      <code>branches:</code> pattern matches every branch, so the dry run names every
      pipeline this would additionally start.
    </span>
  </label>

  <div class="actions">
    <button
      type="button"
      class="btn-secondary"
      data-testid="dry-run-btn"
      disabled={!formComplete || $experimentsStore.estimateLoading}
      on:click={runDryRun}
    >
      {$experimentsStore.estimateLoading ? 'Estimating...' : 'Dry run'}
    </button>

    <button
      type="button"
      class="btn-primary"
      data-testid="launch-btn"
      disabled={!estimateFresh || $experimentsStore.launching}
      on:click={launch}
    >
      {$experimentsStore.launching ? 'Launching...' : 'Launch'}
    </button>

    {#if !estimateFresh}
      <span class="gate-note" data-testid="launch-gate-note">
        Launch unlocks once this exact matrix has been costed.
      </span>
    {/if}
  </div>

  {#if $experimentsStore.estimateError}
    <div class="error-banner" data-testid="dry-run-error">{$experimentsStore.estimateError}</div>
  {/if}
</div>

<style>
  .matrix-builder {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    padding: 1rem;
  }

  .field-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.6rem;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.78rem;
    color: var(--text-muted, #6c7086);
  }

  .cells-readout {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text-color, #cdd6f4);
    padding: 0.35rem 0;
  }

  .axis {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .axis header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .axis h3 {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-color, #cdd6f4);
  }

  .axis-row {
    display: grid;
    grid-template-columns: 9rem 1fr 1fr 2rem;
    gap: 0.4rem;
    align-items: center;
  }

  .verify {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    border-top: 1px solid var(--border-color, #45475a);
    padding-top: 0.7rem;
  }

  .toggle {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    font-size: 0.78rem;
    color: var(--text-muted, #6c7086);
  }

  .toggle input {
    margin-top: 0.15rem;
  }

  .hint {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    border-top: 1px solid var(--border-color, #45475a);
    padding-top: 0.8rem;
  }

  .gate-note {
    font-size: 0.75rem;
    color: var(--warning-color, #fab387);
  }

  .error-banner {
    background: rgba(243, 139, 168, 0.15);
    border: 1px solid var(--error-color, #f38ba8);
    color: var(--error-color, #f38ba8);
    border-radius: 6px;
    padding: 0.5rem 0.8rem;
    font-size: 0.8rem;
  }

  input,
  select {
    padding: 0.4rem 0.5rem;
    background: var(--input-bg, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.82rem;
    font-family: inherit;
    min-width: 0;
  }

  input:focus,
  select:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  code {
    font-size: 0.72rem;
    background: var(--surface-alt, #313244);
    padding: 0 0.2rem;
    border-radius: 3px;
  }

  .btn-primary {
    padding: 0.45rem 1rem;
    border-radius: 6px;
    border: none;
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #1e1e2e);
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
  }

  .btn-secondary {
    padding: 0.45rem 1rem;
    border-radius: 6px;
    border: none;
    background: var(--surface-alt, #313244);
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .btn-small {
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    border: 1px solid var(--border-color, #45475a);
    background: transparent;
    color: var(--text-muted, #6c7086);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .btn-icon {
    background: transparent;
    border: none;
    color: var(--text-muted, #6c7086);
    font-size: 1rem;
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
