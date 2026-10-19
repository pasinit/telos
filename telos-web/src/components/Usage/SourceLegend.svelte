<script lang="ts">
  /**
   * Small legend mapping each cost source to its swatch colour.
   * Driven by `lib/usage.ts::sourceLegend()` so a colour change there
   * propagates here without component edits.
   */
  import { sourceLegend } from '../../lib/usage';

  const legend = sourceLegend();
</script>

<div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-500 dark:text-gray-400 pt-3">
  {#each legend as item (item.src)}
    <span class="inline-flex items-center gap-1.5">
      <span class="inline-block w-2.5 h-2.5 rounded-sm" style="background-color: {item.color};"></span>
      {item.src}
    </span>
  {/each}
</div>
