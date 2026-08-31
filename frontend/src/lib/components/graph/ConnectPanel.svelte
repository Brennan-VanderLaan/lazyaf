<script lang="ts">
  /**
   * Connect two nodes without a mouse drag.
   *
   * WHY THIS EXISTS. Until 12.8 the ONLY way to draw an edge was to drag
   * between two SvelteFlow handles. That has two consequences, and the second
   * is the one that mattered:
   *
   *  1. It is unreachable from a keyboard. The palette already grew an
   *     Enter/Space path for adding a node for exactly this reason; the edge
   *     had none, so a keyboard user could build every step of a pipeline and
   *     then not connect any of them - including to Start, which is how an
   *     entry point is declared, which is what makes a pipeline runnable.
   *  2. SvelteFlow's connection drag does not respond to synthetic pointer
   *     events, so NINE Playwright specs covering edges, edge conditions,
   *     entry points and saving stood permanently skipped - on the only
   *     authoring surface the product has. Nine standing skips is not
   *     coverage, it is a note saying nobody is looking (R4).
   *
   * This is a real affordance, not a test hook: the drag still works, and
   * both paths go through the SAME editor function and the SAME default-
   * condition rule, so neither can drift away from the other (R3).
   */
  import { untrack } from 'svelte';
  import type { EdgeCondition, PipelineEdge as PipelineEdgeType, PipelineStepV2 } from '../../api/types';

  interface Props {
    /** Selectable steps, already in display order. */
    steps: PipelineStepV2[];
    /** Existing edges, for the duplicate check. */
    edges: PipelineEdgeType[];
    /** Existing entry points - a Start connection is one of these, not an edge. */
    entryPoints: string[];
    /** Id of the Start node - a legal SOURCE, never a target. */
    startNodeId: string;
    /** The same default the drag path uses, so the two cannot disagree. */
    defaultConditionFor: (sourceId: string) => EdgeCondition;
    onConnect: (fromStep: string, toStep: string, condition: EdgeCondition) => void;
    onClose: () => void;
  }

  let { steps, edges, entryPoints, startNodeId, defaultConditionFor, onConnect, onClose }: Props = $props();

  const CONDITIONS: { value: EdgeCondition; label: string }[] = [
    { value: 'success', label: 'On success' },
    { value: 'failure', label: 'On failure' },
    { value: 'always', label: 'Always' },
  ];

  // `untrack` says "the INITIAL value, deliberately": the panel is mounted
  // fresh every time it opens ({#if connecting} in the editor), so these are
  // the defaults for this opening and the author owns them from then on -
  // re-seeding them from a prop mid-edit would move the selection under the
  // user's cursor.
  let fromStep = $state<string>(untrack(() => startNodeId));
  let toStep = $state<string>(untrack(() => steps[0]?.id ?? ''));
  // Only ever read once the author has picked a condition, so its seed is
  // arbitrary; the OFFERED value is `condition` below.
  let conditionTouched = $state(false);
  let chosenCondition = $state<EdgeCondition>('success');
  let problem = $state<string | null>(null);

  // Until the user picks a condition themselves, the select MIRRORS the
  // default the source would get from a drag - so what the panel shows and
  // what a drag would produce are the same thing, visibly.
  let condition = $derived(conditionTouched ? chosenCondition : defaultConditionFor(fromStep));

  function labelFor(id: string): string {
    if (id === startNodeId) return 'Start';
    return steps.find((s) => s.id === id)?.name ?? id;
  }

  function submit() {
    problem = null;

    if (!fromStep || !toStep) {
      problem = 'Choose a step at both ends of the connection.';
      return;
    }
    if (fromStep === toStep) {
      problem = `"${labelFor(fromStep)}" cannot be connected to itself.`;
      return;
    }
    if (fromStep === startNodeId) {
      // A Start connection declares an entry point; it is not an edge, so
      // the duplicate check is against entry_points. Saying so is better
      // than silently doing nothing, which is what an idempotent add looks
      // like from the outside.
      if (entryPoints.includes(toStep)) {
        problem = `"${labelFor(toStep)}" is already an entry point.`;
        return;
      }
    } else if (edges.some((e) => e.from_step === fromStep && e.to_step === toStep && e.condition === condition)) {
      problem = `"${labelFor(fromStep)}" already connects to "${labelFor(toStep)}" on ${condition}.`;
      return;
    }

    onConnect(fromStep, toStep, condition);
    onClose();
  }
</script>

<div class="connect-panel" data-testid="connect-panel" role="dialog" aria-label="Connect steps">
  <div class="panel-header">
    <span>Connect steps</span>
    <button class="close-btn" data-testid="connect-close" onclick={onClose} title="Close">&times;</button>
  </div>

  {#if steps.length === 0}
    <p class="panel-empty" data-testid="connect-empty">
      Add a step first - there is nothing to connect yet.
    </p>
  {:else}
    <div class="panel-body">
      <label for="connect-from">From</label>
      <select id="connect-from" data-testid="connect-from" bind:value={fromStep}>
        <option value={startNodeId}>Start</option>
        {#each steps as step (step.id)}
          <option value={step.id}>{step.name}</option>
        {/each}
      </select>

      <label for="connect-to">To</label>
      <select id="connect-to" data-testid="connect-to" bind:value={toStep}>
        {#each steps as step (step.id)}
          <option value={step.id}>{step.name}</option>
        {/each}
      </select>

      <label for="connect-condition">When</label>
      <select
        id="connect-condition"
        data-testid="connect-condition"
        value={condition}
        onchange={(e) => {
          conditionTouched = true;
          chosenCondition = (e.currentTarget as HTMLSelectElement).value as EdgeCondition;
        }}
      >
        {#each CONDITIONS as option (option.value)}
          <option value={option.value}>{option.label}</option>
        {/each}
      </select>
    </div>

    {#if problem}
      <p class="panel-problem" data-testid="connect-problem">{problem}</p>
    {/if}

    <div class="panel-footer">
      <button class="btn secondary" data-testid="connect-cancel" onclick={onClose}>Cancel</button>
      <button class="btn primary" data-testid="connect-confirm" onclick={submit}>Connect</button>
    </div>
  {/if}
</div>

<style>
  .connect-panel {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 20;
    width: 260px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
    font-size: 13px;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-color);
    font-weight: 600;
    color: var(--text-color);
  }

  .close-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-size: 18px;
    line-height: 1;
    cursor: pointer;
  }

  .panel-body {
    display: grid;
    grid-template-columns: 56px 1fr;
    align-items: center;
    gap: 8px;
    padding: 12px;
  }

  .panel-body label {
    color: var(--text-muted);
    font-size: 12px;
  }

  .panel-body select {
    width: 100%;
    padding: 6px 8px;
    background: var(--bg-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    border-radius: 4px;
  }

  .panel-empty {
    margin: 0;
    padding: 12px;
    color: var(--text-muted);
  }

  .panel-problem {
    margin: 0;
    padding: 0 12px 8px;
    color: var(--error-color);
    font-size: 12px;
  }

  .panel-footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    padding: 10px 12px;
    border-top: 1px solid var(--border-color);
  }

  .btn {
    padding: 6px 12px;
    border-radius: 5px;
    border: 1px solid var(--border-color);
    font-size: 12px;
    cursor: pointer;
  }

  .btn.secondary {
    background: var(--surface-alt);
    color: var(--text-color);
  }

  .btn.primary {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--primary-text);
  }
</style>
