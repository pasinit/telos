<script lang="ts">
  /**
   * "By model" table inside one agent's accordion.
   *
   * Sorted largest-first; renders model name + calls + cost. Ports the
   * inline Jinja `<table>` verbatim — model name is allowed to be
   * `null` from the wire payload (legacy events pre-instrumentation).
   */
  import { formatUsd, sortedModels, type AgentRollup } from '../../lib/usage';

  export let agent: AgentRollup;

  $: rows = sortedModels(agent);
</script>

<div>
  <p class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">By model</p>
  <table class="w-full text-xs">
    <thead>
      <tr class="text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
        <th class="text-left font-normal py-1.5 pr-2">Model</th>
        <th class="text-right font-normal py-1.5 px-2">Calls</th>
        <th class="text-right font-normal py-1.5 pl-2">Cost</th>
      </tr>
    </thead>
    <tbody>
      {#each rows as row (row.key)}
        <tr class="border-b border-gray-200/40 dark:border-gray-800/40">
          <td class="py-1.5 pr-2 font-mono text-gray-700 dark:text-gray-200 truncate max-w-[200px] md:max-w-none">{row.key || '(unknown)'}</td>
          <td class="py-1.5 px-2 text-right tabular-nums text-gray-500 dark:text-gray-400">{row.calls}</td>
          <td class="py-1.5 pl-2 text-right tabular-nums text-gray-700 dark:text-gray-200">{formatUsd(row.total_usd, 4)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>
