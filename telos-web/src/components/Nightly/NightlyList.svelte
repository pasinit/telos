<script lang="ts">
  /** The list of nightly-feedback rows, or an empty-state card. */
  import NightlyRow from './NightlyRow.svelte';
  import type { NightlyFeedbackRow } from '../../lib/stores/nightly';

  export let rows: NightlyFeedbackRow[] = [];
</script>

{#if rows.length === 0}
  <div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-gray-200 dark:border-gray-800 text-sm text-gray-500">
    No nightly feedback yet. The analysis runs overnight and reviews the day's conversations.
  </div>
{:else}
  <div class="space-y-3">
    {#each rows as row (row.id)}
      <NightlyRow {row} />
    {/each}
  </div>
{/if}
