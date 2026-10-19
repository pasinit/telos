<script lang="ts">
  /**
   * One agent's detail accordion.
   *
   * Wraps `<details>` + the source / model / recent-events panels.
   * `open` is driven from the parent (top-3 + `?open=` slug); we keep
   * it as a one-way prop and let the user toggle locally via the
   * built-in `<details>` behaviour.
   *
   * The recent-events panel only starts fetching once the accordion is
   * actually open — saves a round-trip per agent on the initial render.
   */
  import { formatUsd, type AgentRollup } from '../../lib/usage';
  import ModelBreakdown from './ModelBreakdown.svelte';
  import RecentEvents from './RecentEvents.svelte';
  import SourceBreakdown from './SourceBreakdown.svelte';

  export let agent: AgentRollup;
  export let defaultOpen = false;

  // Track whether the user has ever opened this accordion so we can
  // gate the lazy fetch in <RecentEvents>. Initialised from
  // `defaultOpen` so top-3 spenders fetch their events on first paint
  // (matching the SSR template behaviour).
  let isOpen = defaultOpen;

  function onToggle(ev: Event): void {
    const el = ev.currentTarget as HTMLDetailsElement;
    isOpen = el.open;
  }

  $: isHousekeeper = agent.slug === '_housekeeper';
</script>

<details
  id="agent-{agent.slug}"
  class="bg-gray-100 dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl overflow-hidden"
  open={defaultOpen}
  on:toggle={onToggle}
>
  <summary class="cursor-pointer select-none px-4 md:px-5 py-3 flex items-center justify-between gap-3 hover:bg-gray-200/60 dark:hover:bg-gray-800/60 transition-colors">
    <span class="font-medium text-sm text-gray-800 dark:text-gray-100 truncate">
      {agent.name}
      {#if isHousekeeper}
        <span class="ml-1 text-[10px] uppercase tracking-wide text-amber-600 dark:text-amber-400">housekeeper</span>
      {/if}
    </span>
    <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums shrink-0">
      {formatUsd(agent.total_usd, 4)}
    </span>
  </summary>
  <div class="px-4 md:px-5 pb-5 pt-1 space-y-5">
    {#if agent.total_usd > 0}
      <SourceBreakdown {agent} />
      <ModelBreakdown {agent} />
      <RecentEvents slug={agent.slug} loadable={isOpen} />
    {:else}
      <p class="text-xs text-gray-500 dark:text-gray-400 italic">No cost events recorded for this window.</p>
    {/if}
  </div>
</details>
