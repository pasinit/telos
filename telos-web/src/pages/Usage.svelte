<script lang="ts">
  /**
   * Usage page (SPA port of `telos/ui/templates/usage.html`).
   *
   * Reads `/api/usage` and renders:
   * - the headline KPI (total spend, calls, avg/call)
   * - the per-agent stacked spend bar list
   * - the agent detail accordions (source / model / recent events)
   *
   * URL is the source of truth for state: `?since=24h|7d|30d|all` and
   * `?open=<slug>` survive refresh / deep-link. Both are normalised via
   * helpers in `lib/usage.ts` so the page component stays declarative.
   */
  import { onMount } from 'svelte';
  import { push, querystring, replace } from 'svelte-spa-router';
  import { ApiError, apiGet } from '../lib/api';
  import {
    agentCallCount,
    autoOpenSlugs,
    avgCostPerCall,
    formatUsd,
    normaliseRange,
    pluraliseCalls,
    totalCalls,
    type RangeToken,
    type UsageRollup,
  } from '../lib/usage';
  import Shell from '../components/Shell.svelte';
  import AgentAccordion from '../components/Usage/AgentAccordion.svelte';
  import AgentSpendBar from '../components/Usage/AgentSpendBar.svelte';
  import KpiBlock from '../components/Usage/KpiBlock.svelte';
  import RangePills from '../components/Usage/RangePills.svelte';
  import SourceLegend from '../components/Usage/SourceLegend.svelte';

  // URL-derived state. `$querystring` is a Svelte store; reading it
  // reactively triggers a re-fetch when the user changes the range.
  let activeRange: RangeToken = 'all';
  let openSlug: string | null = null;

  let state: 'loading' | 'loaded' | 'error' | 'unauth' = 'loading';
  let rollup: UsageRollup | null = null;
  let errMsg = '';

  // Re-resolve URL state every time the querystring changes. Triggers
  // the load reactively below.
  $: {
    const qs = new URLSearchParams($querystring);
    activeRange = normaliseRange(qs.get('since'));
    openSlug = qs.get('open');
  }

  // Re-fetch whenever the active range changes. `loaded`/`error`/`unauth`
  // flip back to `loading` so the spinner returns during the swap.
  $: void loadRollup(activeRange);

  async function loadRollup(range: RangeToken): Promise<void> {
    state = 'loading';
    try {
      const data = await apiGet<UsageRollup>(`/usage?since=${encodeURIComponent(range)}`);
      rollup = data;
      state = 'loaded';
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        state = 'unauth';
        // Send the user to the login page; this page is auth-gated.
        replace('/login');
        return;
      }
      errMsg = e instanceof Error ? e.message : String(e);
      state = 'error';
    }
  }

  function selectRange(detail: RangeToken): void {
    // Preserve `?open=...` across range swaps so a deep-linked accordion
    // stays open when the user toggles the range pills.
    const params = new URLSearchParams();
    params.set('since', detail);
    if (openSlug) params.set('open', openSlug);
    push(`/usage?${params.toString()}`);
  }

  // Reload the current view (the SSR header had a refresh button).
  function refresh(): void {
    void loadRollup(activeRange);
  }

  onMount(() => {
    // First mount also triggers loadRollup via the reactive `$:` above.
  });

  // Derived presentation values.
  $: tCalls = rollup ? totalCalls(rollup) : 0;
  $: avg = rollup ? avgCostPerCall(rollup) : 0;
  $: openSlugs = rollup ? autoOpenSlugs(rollup, openSlug) : new Set<string>();
  $: maxTotal = rollup && rollup.by_agent.length ? rollup.by_agent[0].total_usd : 0;
</script>

<!--
  Wrapped in <Shell> (sidebar + mobile drawer owned by page-1-login per
  PR 7D). The `mobileHeader` slot fragment renders inside the mobile
  header bar next to the drawer toggle; the desktop header (with range
  pills + refresh button) lives inside the default-slot scroll region.
  Shell's main column already does `flex-1 flex flex-col h-full
  min-w-0`, so the page only needs its own `overflow-y-auto` wrapper.
-->
<Shell>
  <svelte:fragment slot="mobileHeader">
    <h1 class="font-semibold text-gray-800 dark:text-gray-100">Usage</h1>
  </svelte:fragment>

  <div class="flex-1 overflow-y-auto">
    <div class="max-w-4xl mx-auto px-4 md:px-8 py-6 md:py-10 space-y-8">
      <header class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div class="flex items-center gap-3">
          <h1 class="text-2xl font-bold">Usage</h1>
        </div>
        <div class="flex items-center gap-2">
          <RangePills active={activeRange} on:select={(e) => selectRange(e.detail)} />
          <button
            type="button"
            on:click={refresh}
            title="Reload"
            class="p-2 rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-gray-500 hover:text-gray-800 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4 h-4">
              <polyline points="23 4 23 10 17 10"></polyline>
              <polyline points="1 20 1 14 7 14"></polyline>
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
            </svg>
          </button>
        </div>
      </header>

      {#if state === 'loading' && !rollup}
        <div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 md:p-8 border border-gray-200 dark:border-gray-800">
          <p class="text-sm text-gray-500 dark:text-gray-400 italic">Loading usage…</p>
        </div>
      {:else if state === 'error'}
        <div class="bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-800 rounded-2xl p-6">
          <p class="text-sm font-semibold text-rose-700 dark:text-rose-300">Couldn't load usage.</p>
          {#if errMsg}<p class="text-xs text-rose-600 dark:text-rose-400 mt-1">{errMsg}</p>{/if}
          <button type="button" on:click={refresh} class="mt-3 text-xs underline text-rose-700 dark:text-rose-300">Retry</button>
        </div>
      {:else if rollup}
        <KpiBlock totalUsd={rollup.total_usd} totalCalls={tCalls} avgCost={avg} range={activeRange} />

        <section class="space-y-4">
          <h2 class="font-semibold text-gray-700 dark:text-gray-200">Per agent</h2>
          {#if rollup.by_agent.length && rollup.total_usd > 0}
            <div class="space-y-3">
              {#each rollup.by_agent as agent (agent.slug)}
                <a
                  href={`/usage?since=${activeRange}&open=${encodeURIComponent(agent.slug)}#agent-${agent.slug}`}
                  on:click|preventDefault={() => {
                    const params = new URLSearchParams();
                    params.set('since', activeRange);
                    params.set('open', agent.slug);
                    push(`/usage?${params.toString()}`);
                    // Best-effort: scroll the accordion into view after the
                    // reactive update applies the open state.
                    queueMicrotask(() => {
                      const el = document.getElementById(`agent-${agent.slug}`);
                      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    });
                  }}
                  class="block group"
                >
                  <AgentSpendBar
                    {agent}
                    {maxTotal}
                    callCount={agentCallCount(agent)}
                    isHousekeeper={agent.slug === '_housekeeper'}
                  />
                </a>
              {/each}
            </div>

            <SourceLegend />
          {:else}
            <p class="text-sm text-gray-500 dark:text-gray-400 italic">No spend in this window yet.</p>
          {/if}
        </section>

        {#if rollup.by_agent.length}
          <section class="space-y-3">
            <h2 class="font-semibold text-gray-700 dark:text-gray-200">Detail</h2>
            {#each rollup.by_agent as agent (agent.slug)}
              <AgentAccordion {agent} defaultOpen={openSlugs.has(agent.slug)} />
            {/each}
          </section>
        {/if}

        <footer class="text-[11px] text-gray-400 dark:text-gray-500 leading-relaxed pt-4 border-t border-gray-200 dark:border-gray-800">
          <p>
            Caveats: costs don't yet account for cache reads/writes
            (over-counts post-caching). Extraction cost lags by one turn.
          </p>
          <p class="mt-1 tabular-nums">{pluraliseCalls(tCalls)} &middot; {formatUsd(rollup.total_usd, 4)} total</p>
        </footer>
      {/if}
    </div>
  </div>
</Shell>
