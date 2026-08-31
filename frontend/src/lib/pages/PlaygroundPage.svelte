<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import {
    playgroundStore,
    isRunning,
    canStart,
    hasResult,
    attachmentGate,
    limitsSentence,
  } from '../stores/playground';
  import { modelsStore, claudeModels, geminiModels, modelsLoading } from '../stores/models';
  // CapabilityChecks is THE capability display (Lane B). Imported, never
  // reimplemented: a second copy is a second place for "never probed" to start
  // quietly looking like "not supported", which is the one thing the six-state
  // vocabulary exists to prevent (R3).
  import { CapabilityChecks, EndpointOptionGroup } from '../components/endpoint';
  import { endpointModelValue, endpointsStore, HARNESS_AGENT } from '../stores/endpoints';
  import { selectedRepoId, selectedRepo } from '../stores/repos';
  import { agentFilesStore } from '../stores/agentFiles';
  import { repos, lazyafFiles } from '../api/client';
  import type { BranchInfo, AgentFile, RepoAgent, MergedAgent } from '../api/types';
  import RawDiffViewer from '../components/RawDiffViewer.svelte';

  let branches: BranchInfo[] = [];
  let branchesLoading = false;
  let repoAgents: RepoAgent[] = [];
  let repoAgentsLoading = false;

  // UI refs
  let logsContainer: HTMLDivElement;

  /**
   * FOLLOW MODE. True while the pane should stay pinned to the newest output.
   *
   * This is the whole of "output wouldn't scroll up". The old code armed a
   * 100ms debounced snap on every incoming line and re-checked only that the
   * container still existed when the timer fired - so a snap armed by the last
   * line before the user scrolled up still yanked them back, and because every
   * new line RE-ARMED the timer it could only ever fire during a pause, which
   * is what a real agent does constantly. The same snap moving content under a
   * held mouse button is also what blew up drag-selections from 73 characters
   * to 1781: the browser correctly extends a live selection to wherever the
   * cursor now points.
   */
  let followTail = true;
  /**
   * When we last scrolled the pane ourselves.
   *
   * A timestamp rather than a boolean flag: a flag set around the assignment
   * has to be cleared by the scroll event it expects, and a browser that
   * coalesces that event away (or a hidden tab, where rAF never runs) leaves
   * it armed to swallow the user's next real scroll. A window expires by
   * itself.
   */
  let programmaticScrollAt = 0;
  const PROGRAMMATIC_SCROLL_WINDOW_MS = 150;
  /** True between pointerdown in the pane and the matching release. */
  let selecting = false;
  /** Distance from the bottom, in px, still counted as "at the bottom". */
  const FOLLOW_THRESHOLD_PX = 24;

  // Timer for the running duration. One plain 1s tick; the label below is a
  // pure function of it, so it actually re-renders (it used to read '0s'
  // forever because a no-argument function call in the template gave Svelte
  // no dependency to invalidate on).
  let nowTick = Date.now();
  let timerInterval: ReturnType<typeof setInterval> | null = null;

  let copyState: 'idle' | 'copied' | 'failed' = 'idle';
  let copyMessage = '';
  let copyResetTimer: ReturnType<typeof setTimeout> | null = null;

  // Load branches when repo changes
  $: if ($selectedRepoId) {
    loadBranches($selectedRepoId);
    loadRepoAgents($selectedRepoId);
    agentFilesStore.load();
  }

  // Get available models based on runner type
  // The harness has no CLI model list of its own - its models are the
  // registered endpoints, which arrive through EndpointOptionGroup below.
  $: availableModels =
    $playgroundStore.runnerType === 'openai-harness'
      ? []
      : $playgroundStore.runnerType === 'claude-code'
        ? $claudeModels
        : $geminiModels;

  // Auto-select first model when runner type changes or models load
  $: {
    // M14: the harness has no CLI model list, so this auto-select must not
    // run for it - otherwise it would stomp a chosen `endpoint:<name>` back
    // to a Claude model the moment the endpoint store settled.
    if ($playgroundStore.runnerType !== 'openai-harness') {
      const models = $playgroundStore.runnerType === 'claude-code' ? $claudeModels : $geminiModels;
      const currentModel = $playgroundStore.model;
      if (models.length > 0) {
        const isValidModel = models.some(m => m.id === currentModel);
        if (!isValidModel) {
          playgroundStore.setConfig({ model: models[0].id as any });
        }
      }
    }
  }

  // -------------------------------------------------------------------------
  // Modalities: what the chosen model can be GIVEN, and what a human may
  // attach here (14.5)
  // -------------------------------------------------------------------------

  /** `endpointsStore.probing` is its own store; `$` needs a local binding. */
  const endpointProbing = endpointsStore.probing;

  /**
   * The endpoint this page is pointed at, or null.
   *
   * Matched by PRODUCING the option value and comparing, never by parsing
   * `endpoint:<name>` apart. `resolve_step_endpoint` is the only parser of
   * that sugar spelling by construction (cross-agent contract #4), and the
   * whole reason `stores/endpoints` exports `endpointModelValue` is so that
   * surfaces like this one can round-trip the value without becoming a second
   * parser that drifts.
   */
  $: selectedEndpoint =
    $playgroundStore.model
      ? $endpointsStore.find((e) => endpointModelValue(e.name) === $playgroundStore.model) ?? null
      : null;

  /** The PLATFORM's answer for images, from GET /api/playground/capabilities. */
  $: imagesPlatform =
    $playgroundStore.capabilities?.modalities?.find((m) => m.modality === 'images') ?? null;

  /**
   * May a human attach an image right now, and if not, WHICH link said no.
   *
   * Every state is a disabled control with its own sentence. The two that must
   * never read alike are `unprobed` ("nobody asked - probe it") and
   * `undetectable` ("the server took the image, returned 200, and the prompt
   * token count did not move"); the second is the dangerous one, because the
   * request succeeds while the input vanishes.
   */
  $: attachGate = attachmentGate({
    runnerType: $playgroundStore.runnerType,
    endpoint: selectedEndpoint,
    platform: imagesPlatform,
  });

  /** The caps, in the SERVER's numbers rather than a second copy of them. */
  $: attachLimits = limitsSentence($playgroundStore.capabilities?.attachment_limits ?? null);

  $: attachAccept = ($playgroundStore.capabilities?.attachment_limits?.media_types ?? []).join(',');

  /** True while this page is pointed at a self-hosted endpoint. */
  $: harnessSelected = $playgroundStore.runnerType === HARNESS_AGENT;

  function handleProbeSelectedEndpoint() {
    if (!selectedEndpoint) return;
    // Never swallowed: the store surfaces a failed REQUEST by throwing, and a
    // probe that reports a broken endpoint is a 200 with a red record, which
    // arrives through the normal store update.
    void endpointsStore.probe(selectedEndpoint.id).catch((e) => {
      console.error('[playground] probe failed', e);
    });
  }

  // Follow new output. Narrow dependency on purpose: `logs` only gets a new
  // identity when log lines actually change, so typing in the task box does
  // not schedule a scroll.
  $: logLines = $playgroundStore.logs;
  $: if (logLines && logsContainer) {
    void keepPinned();
  }

  /**
   * Pin the pane to the bottom, if that is still what the user wants.
   *
   * The intent is re-checked HERE, after the DOM has caught up - not when the
   * scroll was scheduled. That one move is the difference between a log
   * viewer and a fight.
   */
  async function keepPinned() {
    await tick();
    if (!logsContainer || !followTail || selecting || paneHasSelection()) return;
    scrollToBottom();
  }

  /**
   * True while the user has text highlighted inside the output pane.
   *
   * Following the tail past a selection the user just made is how a transcript
   * gets scrolled out from under someone who highlighted it in order to copy
   * it. Clicking anywhere collapses the selection and following resumes.
   */
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

  async function loadBranches(repoId: string) {
    branchesLoading = true;
    try {
      const response = await repos.branches(repoId);
      branches = response.branches;
      // Set default branch if not already set
      if (!$playgroundStore.branch && response.default_branch) {
        playgroundStore.setConfig({ branch: response.default_branch });
      }
    } catch (e) {
      console.error('Failed to load branches:', e);
      branches = [];
    } finally {
      branchesLoading = false;
    }
  }

  async function loadRepoAgents(repoId: string) {
    repoAgentsLoading = true;
    try {
      repoAgents = await lazyafFiles.listAgents(repoId);
    } catch {
      repoAgents = [];
    } finally {
      repoAgentsLoading = false;
    }
  }

  // Merge platform and repo agents for display
  $: mergedAgents = [
    ...$agentFilesStore.map((a): MergedAgent => ({
      id: a.id,
      name: a.name,
      description: a.description,
      content: a.content,
      source: 'platform',
    })),
    ...repoAgents.map((a): MergedAgent => ({
      name: a.name,
      description: a.description,
      prompt_template: a.prompt_template,
      source: 'repo',
    })),
  ];

  function handleAgentChange(event: Event) {
    const select = event.target as HTMLSelectElement;
    const value = select.value;

    if (!value) {
      playgroundStore.setConfig({ agentId: null, repoAgentName: null });
    } else if (value.startsWith('platform:')) {
      playgroundStore.setConfig({ agentId: value.slice(9), repoAgentName: null });
    } else if (value.startsWith('repo:')) {
      playgroundStore.setConfig({ agentId: null, repoAgentName: value.slice(5) });
    }
  }

  function getCurrentAgentValue(): string {
    if ($playgroundStore.agentId) {
      return `platform:${$playgroundStore.agentId}`;
    }
    if ($playgroundStore.repoAgentName) {
      return `repo:${$playgroundStore.repoAgentName}`;
    }
    return '';
  }

  async function handleStartTest() {
    // A run always starts pinned; the user has not scrolled anywhere yet.
    followTail = true;
    try {
      await playgroundStore.startTest();
    } catch (e) {
      // Error is handled in store
    }
  }

  function handleCancel() {
    playgroundStore.cancel();
  }

  function handleReset() {
    playgroundStore.reset();
    // Keep the config but clear results
    if ($selectedRepoId) {
      playgroundStore.setConfig({ repoId: $selectedRepoId });
    }
    followTail = true;
  }

  /**
   * Switching runner also clears the model.
   *
   * Each runner has its own model vocabulary, and keeping the old id across
   * the switch left the Model select painted BLANK (selectedIndex -1) with the
   * run button still enabled - a run launched against a model that is not in
   * the list. The reactive auto-select below fills a valid default back in for
   * the runners that have a CLI model list; `openai-harness` has none, so it
   * shows a placeholder and the run button stays disabled until you pick one.
   */
  function handleRunnerTypeChange(value: string) {
    playgroundStore.setConfig({
      runnerType: value as 'claude-code' | 'gemini' | 'openai-harness',
      model: null,
    });
  }

  function handleLogsScroll() {
    if (!logsContainer) return;
    if (Date.now() - programmaticScrollAt < PROGRAMMATIC_SCROLL_WINDOW_MS) {
      // Our own scroll. Reading follow mode off it is what put the user back
      // on the leash they had just escaped: the snap landed the pane at the
      // bottom, this handler saw "near the bottom" and re-armed following.
      return;
    }
    const { scrollTop, scrollHeight, clientHeight } = logsContainer;
    followTail = scrollHeight - scrollTop - clientHeight <= FOLLOW_THRESHOLD_PX;
  }

  function handleLogsPointerDown() {
    // A press in the pane is the start of a selection until proven otherwise.
    // Nothing scrolls the pane while the button is held.
    selecting = true;
  }

  function handleWindowPointerUp() {
    if (!selecting) return;
    selecting = false;
    // Resume following if the user is still parked at the bottom. keepPinned
    // declines on its own while a selection is standing in the pane.
    if (followTail) void keepPinned();
  }

  async function handleCopyLogs() {
    const text = $playgroundStore.logs.join('\n');
    if (copyResetTimer) clearTimeout(copyResetTimer);
    try {
      await navigator.clipboard.writeText(text);
      copyState = 'copied';
      copyMessage = `Copied ${$playgroundStore.logs.length} lines`;
    } catch (e) {
      // R1: a copy that did not happen must not look like one that did.
      copyState = 'failed';
      copyMessage = `Copy failed: ${e instanceof Error ? e.message : e}`;
    }
    copyResetTimer = setTimeout(() => {
      copyState = 'idle';
      copyMessage = '';
    }, 4000);
  }

  function handleOpenHistory(sessionId: string) {
    followTail = true;
    void playgroundStore.openSession(sessionId);
  }

  function formatWhen(iso: string): string {
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return iso;
    return when.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function getStatusColor(status: string): string {
    switch (status) {
      case 'queued': return 'var(--text-muted)';
      case 'running': return 'var(--warning-color)';
      case 'completed': return 'var(--success-color)';
      case 'failed': return 'var(--error-color)';
      case 'cancelled': return 'var(--text-muted)';
      default: return 'var(--text-muted)';
    }
  }

  function getStatusIcon(status: string): string {
    switch (status) {
      case 'idle': return '';
      case 'queued': return 'Queued';
      case 'running': return 'Running';
      case 'completed': return 'Completed';
      case 'failed': return 'Failed';
      case 'cancelled': return 'Cancelled';
      default: return status;
    }
  }

  /**
   * Render a duration. Pure, and every input is an explicit argument, so the
   * reactive statement below actually re-runs when any of them changes.
   */
  function describeDuration(
    startedAt: Date | null,
    completedAt: Date | null,
    serverSeconds: number | null,
    running: boolean,
    now: number
  ): string {
    let seconds: number | null = null;
    if (running && startedAt) {
      seconds = (now - startedAt.getTime()) / 1000;
    } else if (serverSeconds !== null) {
      // The server timed the run; trust it over two client clock readings.
      seconds = serverSeconds;
    } else if (startedAt && completedAt) {
      seconds = (completedAt.getTime() - startedAt.getTime()) / 1000;
    }
    if (seconds === null) return '';
    const whole = Math.max(0, Math.round(seconds));
    if (whole < 60) return `${whole}s`;
    return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  }

  $: durationLabel = describeDuration(
    $playgroundStore.startedAt,
    $playgroundStore.completedAt,
    $playgroundStore.durationSeconds,
    $isRunning,
    nowTick
  );

  // Set repo ID when selected repo changes, and pull that repo's history.
  $: if ($selectedRepoId && $selectedRepoId !== $playgroundStore.repoId) {
    playgroundStore.setConfig({ repoId: $selectedRepoId });
    void playgroundStore.loadHistory();
  }

  $: terminalStatus =
    $playgroundStore.status === 'completed' ||
    $playgroundStore.status === 'failed' ||
    $playgroundStore.status === 'cancelled';

  /**
   * The prompt that produced the transcript currently on screen.
   *
   * "You cannot see what you asked five minutes ago" was half the history
   * complaint. Which source is authoritative depends on what is being shown:
   * for a run opened from history it is that row; for the run this page
   * started it is what the user typed, because the history list may not have
   * refreshed yet.
   */
  function promptOf(sessionId: string | null): string | null {
    if (!sessionId) return null;
    return (
      $playgroundStore.history.find((run) => run.session_id === sessionId)?.prompt ?? null
    );
  }

  $: shownPrompt = $playgroundStore.viewingSessionId
    ? promptOf($playgroundStore.viewingSessionId)
    : $playgroundStore.ranPrompt ?? promptOf($playgroundStore.sessionId);

  onMount(() => {
    // Load available models
    modelsStore.load();
    // M14: the merged selector needs the endpoint registry too.
    void endpointsStore.load();
    // 14.5: what the PLAYGROUND can carry, as opposed to what an endpoint can
    // receive. Both have to be true before anything may be attached, and a
    // failed read leaves the attach control disabled saying exactly that.
    void playgroundStore.loadCapabilities();

    if ($selectedRepoId) {
      playgroundStore.setConfig({ repoId: $selectedRepoId });
      void playgroundStore.loadHistory();
    }

    // Pick the run back up: after a reload, after a navigation, or in a tab
    // that came back to a run that is still going.
    void playgroundStore.reattach();

    timerInterval = setInterval(() => {
      nowTick = Date.now();
    }, 1000);
  });

  onDestroy(() => {
    // NOT a cancel. This used to call `playgroundStore.cancel()`, which meant
    // that clicking any sidebar link during a run killed the agent container
    // - no prompt, no undo, and the user came back to a "Cancelled" badge for
    // something they never cancelled. The store is a module singleton, so the
    // run survives the page; only the stream is closed, and onMount re-opens
    // it. Cancelling is the Cancel button's job.
    playgroundStore.detach();
    if (timerInterval) {
      clearInterval(timerInterval);
      timerInterval = null;
    }
    if (copyResetTimer) {
      clearTimeout(copyResetTimer);
    }
  });
</script>

<!--
  The release that ends a drag-selection often happens OUTSIDE the pane, so
  the pane's own pointerup would never fire and autoscroll would stay
  suppressed forever.
-->
<svelte:window on:pointerup={handleWindowPointerUp} on:pointercancel={handleWindowPointerUp} />

<div class="playground-page" data-testid="playground-page">
  <header class="page-header">
    <div class="header-left">
      <h1>Agent Playground</h1>
      {#if $selectedRepo}
        <span class="repo-badge">{$selectedRepo.name}</span>
      {/if}
    </div>
    {#if $playgroundStore.status !== 'idle'}
      <div
        class="status-badge"
        data-testid="playground-status"
        style="color: {getStatusColor($playgroundStore.status)}"
      >
        {#if $playgroundStore.status === 'running'}
          <span class="spinner"></span>
        {/if}
        {getStatusIcon($playgroundStore.status)}
        {#if durationLabel}
          <span class="duration" data-testid="playground-duration">({durationLabel})</span>
        {/if}
      </div>
    {/if}
  </header>

  {#if !$selectedRepoId}
    <div class="empty-state">
      <p>Select a repository from the sidebar to use the Agent Playground.</p>
    </div>
  {:else}
    <div class="playground-layout">
      <!-- Configuration Panel -->
      <aside class="config-panel" data-testid="config-panel">
        <h2>Configuration</h2>

        <div class="form-group">
          <label for="branch">Branch</label>
          <select
            id="branch"
            value={$playgroundStore.branch || ''}
            on:change={(e) => playgroundStore.setConfig({ branch: e.currentTarget.value })}
            disabled={$isRunning}
          >
            <option value="">Select branch...</option>
            {#each branches as branch}
              <option value={branch.name}>
                {branch.name} {branch.is_default ? '(default)' : ''}
              </option>
            {/each}
          </select>
          {#if branchesLoading}
            <span class="loading-hint">Loading branches...</span>
          {/if}
        </div>

        <div class="form-group">
          <label for="agent">Agent (optional)</label>
          <select
            id="agent"
            value={getCurrentAgentValue()}
            on:change={handleAgentChange}
            disabled={$isRunning}
          >
            <option value="">No agent (use task only)</option>
            {#if mergedAgents.filter(a => a.source === 'platform').length > 0}
              <optgroup label="Platform Agents">
                {#each mergedAgents.filter(a => a.source === 'platform') as agent}
                  <option value="platform:{agent.id}">{agent.name}</option>
                {/each}
              </optgroup>
            {/if}
            {#if mergedAgents.filter(a => a.source === 'repo').length > 0}
              <optgroup label="Repo Agents">
                {#each mergedAgents.filter(a => a.source === 'repo') as agent}
                  <option value="repo:{agent.name}">{agent.name}</option>
                {/each}
              </optgroup>
            {/if}
          </select>
        </div>

        <div class="form-row">
          <div class="form-group half">
            <label for="runner-type">Runner Type</label>
            <select
              id="runner-type"
              value={$playgroundStore.runnerType}
              on:change={(e) => handleRunnerTypeChange(e.currentTarget.value)}
              disabled={$isRunning}
            >
              <option value="claude-code">Claude Code</option>
              <option value="gemini">Gemini</option>
              <!-- M14: LazyAF supplies the agent loop; the model is one you host. -->
              <option value="openai-harness">Self-hosted endpoint</option>
            </select>
          </div>

          <div class="form-group half">
            <label for="model">Model {#if $modelsLoading}<span class="loading-indicator">(loading...)</span>{/if}</label>
            <select
              id="model"
              data-testid="model-select"
              value={$playgroundStore.model ?? ''}
              on:change={(e) => playgroundStore.setConfig({ model: e.currentTarget.value as any })}
              disabled={$isRunning || ($playgroundStore.runnerType !== 'openai-harness' && ($modelsLoading || availableModels.length === 0))}
            >
              <!--
                A visible placeholder rather than a blank box. Switching runner
                clears the model, and `openai-harness` has no CLI model list at
                all, so without this the select painted empty with no hint that
                a choice was owed - and the run button was still enabled.
              -->
              {#if !$playgroundStore.model}
                <option value="">Select a model...</option>
              {/if}
              {#each availableModels as model}
                <option value={model.id} title={model.description}>{model.name}</option>
              {/each}
              <!--
                M14: self-hosted endpoints merge into the SAME selector as a
                `Self-hosted` optgroup, emitting `endpoint:<name>`. One merged
                list beats two competing ones, and the value it emits is the
                one spelling `resolve_step_endpoint` parses.
              -->
              <EndpointOptionGroup />
            </select>
          </div>
        </div>

        <!--
          WHAT THIS MODEL CAN BE GIVEN (14.5).

          Directly under the model select, because it is an answer about the
          thing that was just chosen and it changes what the box below it may
          contain. The strip itself is `CapabilityChecks` - the SAME component
          the Endpoints page and the endpoint modal render, in its `panel`
          variant. There is deliberately no playground-specific copy of it: a
          second copy is a second place for "never probed" to start looking
          like "not supported".
        -->
        <section class="modality-section" data-testid="playground-modalities">
          <h3>What this model can be given</h3>

          {#if !harnessSelected}
            <!--
              R1, in the quiet direction. Claude Code and Gemini are CLI
              agents driven by /api/models, not endpoint rows, so there is no
              capability record to render for them - and an empty strip beside
              a green one would read as "Claude cannot see images", which is
              false. Say what is actually true instead.
            -->
            <p class="modality-note" data-testid="modality-cli-note">
              <strong>{$playgroundStore.runnerType === 'gemini' ? 'Gemini' : 'Claude Code'}</strong>
              is a CLI agent, not a model endpoint. What it can read is a property
              of that CLI and of the files in the workspace, not of anything LazyAF
              probed - so there is no capability record to show here. A blank strip
              is not a claim that it cannot see images.
            </p>
          {:else if !selectedEndpoint}
            <p class="modality-note" data-testid="modality-no-endpoint">
              Pick a self-hosted endpoint above to see what it accepts. Nothing
              has been asked about this configuration yet.
            </p>
          {:else}
            <CapabilityChecks
              endpoint={selectedEndpoint}
              variant="panel"
              onProbe={handleProbeSelectedEndpoint}
              probing={$endpointProbing.includes(selectedEndpoint.id)}
            />
          {/if}

          <!--
            THE ATTACH CONTROL.

            Rendered in every state and DISABLED with the reason, never hidden.
            That is the same doctrine `toOption` applies to an unusable
            endpoint: a control that vanishes is indistinguishable from a
            feature that does not exist, and someone who cannot see why they
            cannot attach will go looking for the wrong fix.

            It is a BUTTON and not an `<input type="file">` on purpose. A file
            input that is enabled by accident consumes a file and drops it;
            a button that is enabled by accident does nothing. When the
            delivery path lands (see ATTACHMENT_DELIVERY_GAP in
            backend/app/schemas/playground.py) the picker is wired HERE, and
            the gate above already computes when it may open.
          -->
          <div class="attach-block" data-testid="playground-attach">
            <div class="attach-head">
              <button
                type="button"
                class="btn-secondary attach-btn"
                data-testid="attach-images-btn"
                data-enabled={attachGate.enabled ? 'true' : 'false'}
                data-blocked-by={attachGate.blockedBy ?? ''}
                data-modality-state={attachGate.state ?? ''}
                data-accept={attachAccept}
                disabled={!attachGate.enabled || $isRunning}
                title={attachGate.reason}
              >
                Attach images
              </button>
              {#if attachGate.state}
                <span class="attach-state" data-testid="attach-state">
                  images: {attachGate.state.replace('_', ' ')}
                </span>
              {/if}
            </div>

            <p class="attach-reason" data-testid="attach-reason">{attachGate.reason}</p>

            {#if attachGate.next}
              <p class="attach-next" data-testid="attach-next">{attachGate.next}</p>
            {/if}

            <!--
              The limits are the SERVER's numbers, read from
              GET /api/playground/capabilities. A "max 5 MiB" typed in here
              beside a `5 * 1024 * 1024` in the validator is two sources of
              truth for one contract (R3), and the half that drifts is always
              the sentence.
            -->
            <p class="attach-limits" data-testid="attach-limits">{attachLimits}</p>

            {#if $playgroundStore.capabilitiesError}
              <p class="attach-error" data-testid="attach-capabilities-error">
                The playground's own limits could not be read:
                {$playgroundStore.capabilitiesError}
              </p>
            {/if}
          </div>
        </section>

        <div class="form-group">
          <label for="task">Task Description</label>
          <textarea
            id="task"
            name="task"
            data-testid="task-input"
            placeholder="Describe what you want the agent to do..."
            value={$playgroundStore.taskOverride}
            on:input={(e) => playgroundStore.setConfig({ taskOverride: e.currentTarget.value })}
            disabled={$isRunning}
            rows="4"
          ></textarea>
        </div>

        <div class="form-group checkbox-group">
          <label>
            <input
              type="checkbox"
              checked={$playgroundStore.saveToBranch}
              on:change={(e) => playgroundStore.setConfig({ saveToBranch: e.currentTarget.checked })}
              disabled={$isRunning}
            />
            Save changes to branch
          </label>
          {#if $playgroundStore.saveToBranch}
            <input
              type="text"
              placeholder="Branch name (e.g., playground/test-1)"
              value={$playgroundStore.saveBranchName}
              on:input={(e) => playgroundStore.setConfig({ saveBranchName: e.currentTarget.value })}
              disabled={$isRunning}
              class="branch-name-input"
            />
          {/if}
        </div>

        <div class="button-group">
          {#if $canStart}
            <button
              class="btn-primary"
              data-testid="start-test-btn"
              on:click={handleStartTest}
              disabled={!$playgroundStore.branch ||
                !$playgroundStore.taskOverride.trim() ||
                !$playgroundStore.model}
            >
              Test Once
            </button>
          {:else if $isRunning}
            <button class="btn-danger" data-testid="cancel-btn" on:click={handleCancel}>
              Cancel
            </button>
          {/if}

          {#if $hasResult}
            <button class="btn-secondary" data-testid="reset-btn" on:click={handleReset}>
              Reset
            </button>
          {/if}
        </div>

        <!--
          HISTORY. Not a new store: every playground run already leaves a
          durable PipelineRun behind it, and this lists them. Clicking one
          re-opens its transcript.
        -->
        <section class="history-section" data-testid="playground-history">
          <div class="history-header">
            <h3>Recent runs</h3>
            <button
              class="btn-small"
              on:click={() => playgroundStore.loadHistory()}
              disabled={$playgroundStore.historyLoading}
            >
              {$playgroundStore.historyLoading ? 'Loading...' : 'Refresh'}
            </button>
          </div>

          {#if $playgroundStore.historyError}
            <div class="error-message" data-testid="history-error">
              Could not load past runs: {$playgroundStore.historyError}
            </div>
          {:else if $playgroundStore.history.length === 0}
            <p class="history-empty">
              {$playgroundStore.historyLoading
                ? 'Loading past runs...'
                : 'No runs yet. Your runs are kept here so you can re-read them later.'}
            </p>
          {:else}
            <ul class="history-list">
              {#each $playgroundStore.history as run (run.session_id)}
                <li>
                  <button
                    class="history-item"
                    class:active={$playgroundStore.sessionId === run.session_id}
                    data-testid="history-item"
                    title={run.prompt}
                    on:click={() => handleOpenHistory(run.session_id)}
                  >
                    <span class="history-prompt">{run.prompt}</span>
                    <span class="history-meta">
                      <span style="color: {getStatusColor(run.status)}"
                        >{getStatusIcon(run.status)}</span
                      >
                      <span>{formatWhen(run.created_at)}</span>
                      {#if run.agent}<span>{run.agent}</span>{/if}
                    </span>
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
        </section>
      </aside>

      <!-- Output Panel -->
      <main class="output-panel">
        <!--
          The failure goes WHERE THE USER IS LOOKING. It used to render only
          in the 320px config panel, below its scroll edge, while the output
          pane - two thirds of the screen - still read "output will appear
          here when you run a test". The backend's message is genuinely good
          ("...set ANTHROPIC_API_KEY in the backend's environment"); the UI
          was hiding it.
        -->
        {#if $playgroundStore.error}
          <div class="error-message" data-testid="playground-error">
            {$playgroundStore.error}
          </div>
        {/if}

        <!-- Logs Section -->
        <section class="logs-section">
          <div class="section-header">
            <div class="section-title">
              <h3>Agent Output</h3>
              {#if shownPrompt}
                <p class="shown-prompt" data-testid="shown-prompt" title={shownPrompt}>
                  {shownPrompt}
                </p>
              {/if}
            </div>
            <div class="section-actions">
              {#if copyMessage}
                <span
                  class="copy-message"
                  class:failed={copyState === 'failed'}
                  data-testid="copy-message">{copyMessage}</span
                >
              {/if}
              {#if $playgroundStore.logs.length > 0}
                <button
                  class="btn-small"
                  data-testid="copy-logs-btn"
                  on:click={handleCopyLogs}
                >
                  Copy
                </button>
                <button class="btn-small" on:click={() => playgroundStore.clearLogs()}>
                  Clear
                </button>
              {/if}
            </div>
          </div>
          <div
            class="logs-container"
            data-testid="logs-container"
            bind:this={logsContainer}
            on:scroll={handleLogsScroll}
            on:pointerdown={handleLogsPointerDown}
          >
            {#if $playgroundStore.logs.length === 0}
              <div class="logs-empty">
                {#if $isRunning}
                  <span class="spinner"></span>
                  <span>Waiting for output...</span>
                {:else if terminalStatus}
                  <span>This run produced no output.</span>
                {:else}
                  <span>Agent output will appear here when you run a test.</span>
                {/if}
              </div>
            {:else}
              <!--
                NOT KEYED, deliberately. This block only ever APPENDS, and
                Svelte index-reconciles it without touching existing text
                nodes - so a selection over earlier lines survives new output
                arriving. Keying it would replace nodes on every flush and
                CREATE the selection bug this page was reported for.
              -->
              {#each $playgroundStore.logs as log}
                <div class="log-line">{log}</div>
              {/each}
            {/if}
          </div>
          {#if !followTail && $playgroundStore.logs.length > 0}
            <button
              class="follow-resume"
              data-testid="follow-resume"
              on:click={() => {
                followTail = true;
                void keepPinned();
              }}
            >
              Jump to newest output
            </button>
          {/if}
        </section>

        <!-- Changes -->
        {#if $hasResult}
          <section class="diff-section" data-testid="diff-section">
            <div class="section-header">
              <h3>Changes</h3>
            </div>
            {#if $playgroundStore.diff || $playgroundStore.filesChanged.length > 0}
              <RawDiffViewer
                diff={$playgroundStore.diff || ''}
                filesChanged={$playgroundStore.filesChanged}
              />
            {:else if $playgroundStore.resultSource === 'run'}
              <!--
                R1: "no diff on this record" is NOT "the agent changed
                nothing", and rendering the second sentence for the first
                situation is exactly the silent loss the house rules forbid.
              -->
              <div class="diff-empty" data-testid="diff-not-retained">
                This transcript was restored from the run record. Its diff is not
                retained - a playground branch is deleted once its diff has been
                computed. Tick "Save changes to branch" to keep one next time.
              </div>
            {:else}
              <div class="diff-empty" data-testid="diff-empty">
                {#if $playgroundStore.status === 'cancelled'}
                  The run was cancelled before it reported any changes.
                {:else if $playgroundStore.status === 'failed'}
                  The run failed before it reported any changes.
                {:else}
                  No changes were made by the agent.
                {/if}
              </div>
            {/if}
          </section>
        {/if}
      </main>
    </div>
  {/if}
</div>

<style>
  .playground-page {
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 1rem;
    gap: 1rem;
  }

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .header-left h1 {
    margin: 0;
    font-size: 1.5rem;
    color: var(--text-color);
  }

  .repo-badge {
    background: var(--badge-bg);
    padding: 0.25rem 0.75rem;
    border-radius: 1rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .status-badge {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 500;
  }

  .duration {
    color: var(--text-muted);
    font-weight: normal;
  }

  .empty-state {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1;
    color: var(--text-muted);
  }

  .playground-layout {
    display: flex;
    gap: 1rem;
    flex: 1;
    min-height: 0;
  }

  .config-panel {
    width: 320px;
    min-width: 280px;
    background: var(--surface-color);
    border-radius: 8px;
    padding: 1rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
    overflow-y: auto;
  }

  .config-panel h2 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--text-color);
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .form-row {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  /*
    Runner Type and Model sat side by side in a 320px panel, so the Model
    select overflowed the panel's right edge and clipped its own label - you
    could not read which model you had picked ("Claude Sonnet" of "Claude
    Sonnet 4.5"). A 12rem basis makes the pair WRAP at this panel width, so
    each control gets the full width and the label is readable; they pair up
    again if the panel is ever given more room.
  */
  .form-group.half {
    flex: 1 1 12rem;
    /* Flex items refuse to shrink below their content without this. */
    min-width: 0;
  }

  .form-group label {
    font-size: 0.9rem;
    color: var(--text-muted);
  }

  .loading-indicator {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-style: italic;
  }

  .form-group select,
  .form-group input[type="text"],
  .form-group textarea {
    background: var(--input-bg);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 0.5rem 0.75rem;
    color: var(--text-color);
    font-size: 0.9rem;
    /*
      A <select> will not shrink below its widest option unless told to: the
      Model control overflowed the 320px panel and clipped its own label
      ("Claude Sonnet" of "Claude Sonnet 4.5"). min-width:0 lets it fit.
    */
    min-width: 0;
    width: 100%;
    box-sizing: border-box;
  }

  .form-group select:focus,
  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .form-group select:disabled,
  .form-group input:disabled,
  .form-group textarea:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .form-group textarea {
    resize: vertical;
    min-height: 80px;
  }

  .checkbox-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    color: var(--text-color);
  }

  .checkbox-group input[type="checkbox"] {
    width: 16px;
    height: 16px;
    accent-color: var(--primary-color);
  }

  .branch-name-input {
    margin-top: 0.5rem;
  }

  .loading-hint {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-style: italic;
  }

  /* Modalities (14.5) ----------------------------------------------------- */

  .modality-section {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--badge-bg);
  }

  .modality-section h3 {
    margin: 0;
    font-size: 0.85rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-muted);
  }

  .modality-note {
    margin: 0;
    font-size: 0.8rem;
    line-height: 1.45;
    color: var(--text-muted);
  }

  .attach-block {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--border-color);
  }

  .attach-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .attach-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .attach-state {
    font-size: 0.75rem;
    font-variant: small-caps;
    color: var(--text-muted);
  }

  /* The reason is not a tooltip. A control disabled for a reason nobody wrote
     down on screen is the failure this whole section exists to avoid, and a
     `title` is invisible to anyone not holding a mouse over it. */
  .attach-reason {
    margin: 0;
    font-size: 0.78rem;
    line-height: 1.45;
    color: var(--text-color);
  }

  .attach-next {
    margin: 0;
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--warning-color);
  }

  .attach-limits {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .attach-error {
    margin: 0;
    font-size: 0.75rem;
    color: var(--error-color);
  }

  .button-group {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .btn-primary,
  .btn-secondary,
  .btn-danger,
  .btn-small {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: all 0.2s;
  }

  .btn-primary {
    background: var(--primary-color);
    color: var(--primary-text);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-secondary {
    background: var(--badge-bg);
    color: var(--text-color);
  }

  .btn-secondary:hover {
    background: var(--hover-color);
  }

  .btn-danger {
    background: var(--error-color);
    color: white;
  }

  .btn-danger:hover {
    filter: brightness(1.1);
  }

  .btn-small {
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    background: var(--badge-bg);
    color: var(--text-muted);
  }

  .btn-small:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .error-message {
    padding: 0.75rem;
    background: rgba(243, 139, 168, 0.1);
    border: 1px solid var(--error-color);
    border-radius: 6px;
    color: var(--error-color);
    font-size: 0.9rem;
  }

  .output-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 1rem;
    min-width: 0;
  }

  .logs-section,
  .diff-section {
    background: var(--surface-color);
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /*
    On a page called a playground, the agent's words are the point. These
    weights used to be the other way round, so an 8-line transcript (with its
    top line clipped mid-glyph) sat above a one-line diff that got twice the
    height.
  */
  .logs-section {
    flex: 2;
    min-height: 220px;
    position: relative;
  }

  .diff-section {
    flex: 1;
    min-height: 160px;
    overflow-y: auto;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border-color);
  }

  .section-header h3 {
    margin: 0;
    font-size: 1rem;
    color: var(--text-color);
  }

  .logs-container {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem;
    font-family: 'Fira Code', 'Monaco', 'Consolas', monospace;
    font-size: 0.85rem;
    line-height: 1.5;
  }

  .logs-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    height: 100%;
    color: var(--text-muted);
    font-style: italic;
  }

  .log-line {
    white-space: pre-wrap;
    word-break: break-all;
    padding: 0.1rem 0.5rem;
    color: var(--text-color);
  }

  .log-line:hover {
    background: var(--hover-color);
  }

  .diff-empty {
    padding: 1rem;
    color: var(--text-muted);
    font-style: italic;
    text-align: center;
  }

  .section-title {
    min-width: 0;
  }

  .shown-prompt {
    margin: 0.15rem 0 0;
    font-size: 0.8rem;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .section-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .copy-message {
    font-size: 0.8rem;
    color: var(--success-color);
  }

  .copy-message.failed {
    color: var(--error-color);
  }

  /* A visible way back to the tail, so "stop yanking the user" does not
     become "strand the user". */
  .follow-resume {
    position: absolute;
    bottom: 0.75rem;
    right: 1rem;
    padding: 0.3rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    background: var(--badge-bg);
    color: var(--text-color);
    font-size: 0.78rem;
    cursor: pointer;
  }

  .follow-resume:hover {
    background: var(--hover-color);
  }

  .history-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    border-top: 1px solid var(--border-color);
    padding-top: 0.75rem;
  }

  .history-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .history-header h3 {
    margin: 0;
    font-size: 0.95rem;
    color: var(--text-color);
  }

  .history-empty {
    margin: 0;
    font-size: 0.82rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  .history-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .history-item {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    width: 100%;
    text-align: left;
    padding: 0.45rem 0.55rem;
    border: 1px solid transparent;
    border-radius: 6px;
    background: var(--badge-bg);
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.82rem;
  }

  .history-item:hover {
    background: var(--hover-color);
  }

  .history-item.active {
    border-color: var(--primary-color);
  }

  .history-prompt {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .history-meta {
    display: flex;
    gap: 0.5rem;
    font-size: 0.72rem;
    color: var(--text-muted);
  }

  .spinner {
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid var(--border-color);
    border-top-color: var(--primary-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
