<script lang="ts">
  /**
   * Recent-events panel inside one agent's accordion.
   *
   * Lazy-loaded: fetches the events the first time `loadable=true`
   * flips (i.e. parent accordion opened). Subsequent toggles don't
   * refetch — mirrors the SSR `data-loaded` flag.
   *
   * Wire: `GET /api/agents/{slug}/costs/recent?limit=10` — the Phase-7I
   * alias added so SPA auth failures surface as 401 (not 303).
   */
  import { onMount } from 'svelte';
  import { ApiError, apiGet } from '../../lib/api';
  import { formatUsd, type CostEvent, type RecentEventsResponse } from '../../lib/usage';

  export let slug: string;
  export let loadable = false;

  let state: 'idle' | 'loading' | 'empty' | 'loaded' | 'error' | 'unauth' = 'idle';
  let events: CostEvent[] = [];

  async function load(): Promise<void> {
    if (state !== 'idle') return;
    state = 'loading';
    try {
      const body = await apiGet<RecentEventsResponse>(`/agents/${encodeURIComponent(slug)}/costs/recent?limit=10`);
      events = body.events ?? [];
      state = events.length === 0 ? 'empty' : 'loaded';
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) state = 'unauth';
      else state = 'error';
    }
  }

  // Trigger on first open. The guard inside `load()` keeps us idempotent
  // across reactive re-runs.
  $: if (loadable) void load();

  onMount(() => {
    if (loadable) void load();
  });

  function fmtTs(ts: string): string {
    return (ts || '').slice(0, 16).replace('T', ' ');
  }
</script>

<div>
  <p class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">Recent events</p>
  <div class="recent-events text-xs text-gray-500 dark:text-gray-400">
    {#if state === 'idle' || state === 'loading'}
      <span class="italic">Loading…</span>
    {:else if state === 'unauth'}
      <span class="italic">Session expired — sign in again.</span>
    {:else if state === 'error'}
      <span class="italic">Failed to load.</span>
    {:else if state === 'empty'}
      <span class="italic">No events.</span>
    {:else}
      <table class="w-full text-[11px]">
        <tbody>
          {#each events as e (e.ts + e.source + (e.model ?? ''))}
            <tr class="border-b border-gray-200/40 dark:border-gray-800/40">
              <td class="py-1 pr-2 tabular-nums text-gray-500">{fmtTs(e.ts)}</td>
              <td class="py-1 px-2 font-mono">{e.source}</td>
              <td class="py-1 px-2 font-mono text-gray-500 truncate max-w-[160px]">{e.model ?? ''}</td>
              <td class="py-1 pl-2 text-right tabular-nums">{formatUsd(e.cost_usd, 6)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</div>
