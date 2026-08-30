<script lang="ts">
  /**
   * A `Self-hosted` `<optgroup>`, dropped INSIDE an existing model `<select>`.
   *
   * This is the piece that makes every "choose a model" surface — the card
   * modal, the playground, the pipeline step form and the 12.6.5 experiment
   * matrix — able to name a self-hosted endpoint without any of them growing
   * their own endpoint logic. The option VALUE is `endpoint:<name>`, which is
   * the one sugar spelling `resolve_step_endpoint` parses, so a value emitted
   * here reaches the dispatcher from all four surfaces with zero schema
   * changes anywhere.
   *
   * Usage — three lines at the bottom of any existing select:
   *
   *   <select bind:value={model}>
   *     ...existing Anthropic / Google options...
   *     <EndpointOptionGroup />
   *   </select>
   *
   * Unusable endpoints are LISTED AND DISABLED with the reason, never
   * filtered out: an absent option is indistinguishable from an endpoint that
   * was never registered, and an operator who cannot see why theirs is
   * missing will re-register it instead of probing it.
   */
  import { onMount } from 'svelte';
  import { endpointOptions, endpointsStore } from '../../stores/endpoints';

  interface Props {
    /** Group heading. Overridable so a surface can say "Self-hosted (GPU)". */
    label?: string;
    /** Fetch the registry on mount when the host page has not already. */
    autoload?: boolean;
  }

  let { label = 'Self-hosted', autoload = true }: Props = $props();

  onMount(() => {
    if (autoload && !$endpointsStore.length) {
      void endpointsStore.load();
    }
  });
</script>

{#if $endpointOptions.length > 0}
  <optgroup {label} data-testid="endpoint-optgroup">
    {#each $endpointOptions as option (option.value)}
      <option
        value={option.value}
        disabled={option.disabled}
        title={option.title}
        data-testid="endpoint-option"
        data-endpoint={option.name}
        data-disabled-reason={option.disabledReason}
      >
        {option.label}
      </option>
    {/each}
  </optgroup>
{/if}
