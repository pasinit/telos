<script lang="ts">
  /**
   * Per-agent stacked spend bar (the "sparkline" the brief asks for).
   *
   * The outer bar's width is the agent's spend relative to the top
   * spender; the inner segments add up to 100% of that agent's own
   * spend, coloured per source. Identical visual to the SSR template.
   *
   * Click forwards via `on:click` so the parent can navigate to the
   * detail accordion + set `?open=<slug>`.
   */
  import { formatUsd, pluraliseCalls, spendSegments, type AgentRollup } from '../../lib/usage';

  export let agent: AgentRollup;
  /** Top spender's `total_usd` — used to scale the outer bar width. */
  export let maxTotal: number;
  /** Total call count across all sources (precomputed for tooltip). */
  export let callCount: number;
  /** True for the housekeeper row → render the small uppercase pill. */
  export let isHousekeeper = false;

  $: segments = spendSegments(agent);
  $: barWidthPct = maxTotal > 0 ? (agent.total_usd / maxTotal) * 100 : 0;
</script>

<div class="block group">
  <div class="flex items-center justify-between gap-3 mb-1.5">
    <span class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">
      {agent.name}
      {#if isHousekeeper}
        <span class="ml-1 text-[10px] uppercase tracking-wide text-amber-600 dark:text-amber-400">housekeeper</span>
      {/if}
    </span>
    <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums shrink-0">
      {formatUsd(agent.total_usd, 4)} &middot; {pluraliseCalls(callCount)}
    </span>
  </div>
  <div
    class="h-3 bg-gray-200 dark:bg-gray-800 rounded-full overflow-hidden flex"
    style="width: {barWidthPct.toFixed(2)}%; min-width: 4px;"
  >
    {#each segments as seg (seg.src)}
      <div
        class="h-full"
        style="width: {seg.widthPct.toFixed(4)}%; background-color: {seg.color};"
        title="{seg.src}: {formatUsd(seg.total_usd, 4)} ({pluraliseCalls(seg.calls)})"
      ></div>
    {/each}
  </div>
</div>
