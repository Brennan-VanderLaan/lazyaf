<script lang="ts">
  /**
   * A standalone endpoint picker: the whole `<select>`, for surfaces that
   * swap the model dropdown out entirely once `openai-harness` is chosen
   * (the pipeline step form, and the experiment matrix's self-hosted row).
   *
   * Where a surface already HAS a model dropdown it should keep it and drop
   * in `EndpointOptionGroup` instead — one merged list beats two competing
   * ones.
   *
   * `value` is the `model` field value, i.e. `endpoint:<name>`, so a caller
   * stores exactly what it would have stored for an API model and nothing
   * downstream needs a second code path.
   */
  import { onMount } from 'svelte';
  import { endpointOptions, endpointsStore } from '../../stores/endpoints';

  interface Props {
    /** `endpoint:<name>` or '' for "none chosen". */
    value: string;
    onChange: (value: string) => void;
    id?: string;
    disabled?: boolean;
    testid?: string;
    placeholder?: string;
    autoload?: boolean;
  }

  let {
    value,
    onChange,
    id = 'endpoint-select',
    disabled = false,
    testid = 'endpoint-select',
    placeholder = 'Select an endpoint…',
    autoload = true,
  }: Props = $props();

  onMount(() => {
    if (autoload && !$endpointsStore.length) {
      void endpointsStore.load();
    }
  });

  let selected = $derived($endpointOptions.find((o) => o.value === value) ?? null);
  let empty = $derived($endpointOptions.length === 0);
</script>

<div class="endpoint-select">
  <select
    {id}
    data-testid={testid}
    {disabled}
    {value}
    onchange={(e) => onChange(e.currentTarget.value)}
  >
    <option value="">{empty ? 'No endpoints registered' : placeholder}</option>
    {#each $endpointOptions as option (option.value)}
      <option
        value={option.value}
        disabled={option.disabled}
        title={option.title}
        data-testid="endpoint-option"
        data-endpoint={option.name}
      >
        {option.label}
      </option>
    {/each}
  </select>

  {#if empty}
    <!-- R1: an empty dropdown with no explanation reads as a broken page. -->
    <small class="hint" data-testid="endpoint-select-empty">
      No model endpoints are registered. Add one on the Endpoints page — there is deliberately no
      default endpoint, because guessing which GPU to bill is not a recoverable mistake.
    </small>
  {:else if selected?.disabled}
    <small class="hint bad" data-testid="endpoint-select-warning">
      {selected.name} is currently <strong>{selected.disabledReason}</strong>; a step aimed at it
      will be refused at dispatch.
    </small>
  {/if}
</div>

<style>
  .endpoint-select {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  select {
    background: var(--input-bg);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    padding: 0.5rem 0.6rem;
    font-size: 0.9rem;
    width: 100%;
  }

  .hint {
    font-size: 0.72rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  .hint.bad {
    color: var(--warning-color);
  }
</style>
