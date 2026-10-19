<script lang="ts">
  /**
   * Range selector pill group (24h / 7d / 30d / All).
   *
   * Pure presentation — emits a `select` event with the chosen token.
   * The parent owns the route push so the URL stays the source of
   * truth (deep-linkable, refresh-safe).
   */
  import { createEventDispatcher } from 'svelte';
  import { RANGE_OPTIONS, type RangeToken } from '../../lib/usage';

  export let active: RangeToken;

  const dispatch = createEventDispatcher<{ select: RangeToken }>();
</script>

<div class="inline-flex rounded-lg border border-gray-200 dark:border-gray-800 overflow-hidden text-sm">
  {#each RANGE_OPTIONS as opt (opt.token)}
    <button
      type="button"
      on:click={() => dispatch('select', opt.token)}
      class="px-3 py-2 transition-colors {active === opt.token
        ? 'bg-brand text-white'
        : 'bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'}"
    >{opt.label}</button>
  {/each}
</div>
