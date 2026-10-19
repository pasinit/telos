<script lang="ts">
  /**
   * "By source" panel inside one agent's accordion.
   *
   * Renders one row per source: swatch + name + relative bar + $ + calls.
   * Ports the inline Jinja loop from `usage.html` verbatim.
   */
  import {
    formatUsd,
    pluraliseCalls,
    sortedSources,
    sourceColor,
    type AgentRollup,
  } from '../../lib/usage';

  export let agent: AgentRollup;

  $: rows = sortedSources(agent);
</script>

<div>
  <p class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">By source</p>
  <div class="space-y-1.5">
    {#each rows as row (row.key)}
      {@const pct = agent.total_usd > 0 ? (row.total_usd / agent.total_usd) * 100 : 0}
      {@const color = sourceColor(row.key)}
      <div class="flex items-center gap-3 text-xs">
        <span class="inline-block w-2.5 h-2.5 rounded-sm shrink-0" style="background-color: {color};"></span>
        <span class="font-mono text-gray-700 dark:text-gray-200 w-32 md:w-40 truncate shrink-0">{row.key}</span>
        <div class="flex-1 h-2 bg-gray-200 dark:bg-gray-800 rounded-full overflow-hidden">
          <div class="h-full" style="width: {pct.toFixed(4)}%; background-color: {color};"></div>
        </div>
        <span class="tabular-nums text-gray-500 dark:text-gray-400 w-24 text-right shrink-0">
          {formatUsd(row.total_usd, 4)}
        </span>
        <span class="tabular-nums text-gray-400 dark:text-gray-500 w-16 text-right shrink-0 hidden md:inline">
          {pluraliseCalls(row.calls)}
        </span>
      </div>
    {/each}
  </div>
</div>
