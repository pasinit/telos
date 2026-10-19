<script lang="ts">
  /**
   * Pending-agent confirmation pill. The housekeeper proposes a new
   * specialist mid-turn; clicking Create POSTs to
   * `/api/agents/create` and swaps in a "Created" confirmation.
   *
   * Ports the agent half of `telos/ui/static/js/routines.js` (the
   * `addPendingAgent` / `confirmCreateAgent` flow).
   */
  import { apiPost } from '../../lib/api';
  import type { PendingAgentEvent } from './stream';
  import { link } from 'svelte-spa-router';

  export let agent: PendingAgentEvent;

  type Status = 'pending' | 'created' | 'dismissed' | 'error';
  let status: Status = 'pending';
  let busy = false;
  let createdSlug: string | null = null;
  let createdName: string | null = null;

  $: shortName = agent.name.length > 40 ? agent.name.slice(0, 37) + '…' : agent.name;
  $: shortGoal = agent.goal.length > 120 ? agent.goal.slice(0, 117) + '…' : agent.goal;

  async function create(): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      const resp = await apiPost<{ ok: boolean; slug: string; name: string }>(
        '/agents/create',
        { name: agent.name, goal: agent.goal },
      );
      if (resp?.ok) {
        createdSlug = resp.slug;
        createdName = resp.name;
        status = 'created';
        // The new specialist appears in the sidebar on next page nav.
        // 7E doesn't own the shared `lib/stores/agents.ts` (7D/7F do)
        // — touching it would create a cross-PR shape conflict. The
        // SSR version had the same UX (no live sidebar refresh on
        // create); the post-rebase Shell uses 7F's `resetAgents()`
        // to live-update.
      } else {
        status = 'error';
      }
    } catch {
      status = 'error';
    } finally {
      busy = false;
    }
  }

  function dismiss(): void {
    status = 'dismissed';
  }
</script>

{#if status === 'pending'}
  <div
    class="self-start flex flex-col gap-2 px-4 py-3 rounded-xl bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-700/50 text-sm max-w-[90%] md:max-w-[75%]"
  >
    <div class="font-medium" style="color:#b8941f;">New specialist: {shortName}</div>
    <div class="text-xs text-amber-600 dark:text-amber-400">{shortGoal}</div>
    <div class="flex gap-2 mt-1">
      <button
        type="button"
        on:click={create}
        disabled={busy}
        class="bg-amber-600 hover:bg-amber-500 transition-colors rounded-lg px-3 py-1 text-xs font-semibold text-white disabled:opacity-60"
      >
        {busy ? 'Creating…' : 'Create'}
      </button>
      <button
        type="button"
        on:click={dismiss}
        class="bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 transition-colors rounded-lg px-3 py-1 text-xs text-gray-600 dark:text-gray-400"
      >
        Dismiss
      </button>
    </div>
  </div>
{:else if status === 'created' && createdSlug && createdName}
  <div
    class="self-start flex flex-col gap-2 px-4 py-3 rounded-xl bg-amber-50 dark:bg-amber-900/20 border border-amber-300 dark:border-amber-700/50 text-sm max-w-[90%] md:max-w-[75%]"
  >
    <span class="text-xs font-medium" style="color:#b8941f;"
      >✓ Created <a href="/agents/{createdSlug}" use:link class="underline">{createdName}</a></span
    >
  </div>
{:else if status === 'error'}
  <div
    class="self-start flex flex-col gap-2 px-4 py-3 rounded-xl bg-red-50 dark:bg-red-900/30 border border-red-300 dark:border-red-700/50 text-sm max-w-[90%] md:max-w-[75%]"
  >
    <span class="text-red-700 dark:text-red-300 text-xs font-medium">Failed to create agent</span>
  </div>
{/if}
