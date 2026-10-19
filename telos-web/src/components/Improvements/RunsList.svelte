<script lang="ts">
  /**
   * The vertical list of improvement-run rows, or the empty-state card
   * when the agent has no runs yet.
   */
  import RunRow from './RunRow.svelte';
  import type { ImprovementRun } from '../../lib/stores/improvements';

  export let runs: ImprovementRun[] = [];
</script>

{#if runs.length === 0}
  <div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-gray-200 dark:border-gray-800 text-sm text-gray-500">
    No improvement runs recorded yet. The improver kicks off every 20 turns.
  </div>
{:else}
  <div class="space-y-3">
    {#each runs as run (run.id)}
      <RunRow {run} />
    {/each}
  </div>
{/if}
