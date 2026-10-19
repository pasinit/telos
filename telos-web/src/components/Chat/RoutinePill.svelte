<script lang="ts">
  /**
   * Pending-routine confirmation pill. The agent proposes a recurring
   * task during a turn; the user clicks Activate / Dismiss / Update on
   * the pill, which POSTs to the routine endpoints and swaps to a
   * confirmation message in place.
   *
   * Ports the routine half of `telos/ui/static/js/routines.js` (rendering
   * + the activate/dismiss handlers — the pending-agent pill lives in
   * `AgentPill.svelte`).
   */
  import { apiPost, ApiError } from '../../lib/api';
  import type { PendingRoutineEvent } from './stream';

  export let slug: string;
  export let routine: PendingRoutineEvent;

  type Status = 'pending' | 'activated' | 'dismissed' | 'error';
  let status: Status = 'pending';
  let notifyEmail = !!routine.notify_email;
  let busy = false;

  function fmtInterval(s: number): string {
    if (s < 60) return `every ${s}s`;
    if (s < 3600) return `every ${Math.round(s / 60)} min`;
    if (s < 86400) return `every ${Math.round(s / 3600)} hour${s >= 7200 ? 's' : ''}`;
    return `every ${Math.round(s / 86400)} day${s >= 172800 ? 's' : ''}`;
  }

  function truncate(s: string, n: number): string {
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  $: shortDesc = truncate((routine.description ?? '').split('\n')[0], 80);
  $: freq = fmtInterval(routine.interval_seconds);
  $: timeOfDay = routine.time_of_day ? ` at ${routine.time_of_day}` : '';
  $: once = routine.one_shot ? ' (once)' : '';

  async function activate(): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      await apiPost(`/agents/${slug}/routines/${routine.id}/activate`, {
        notify_email: notifyEmail,
      });
      status = 'activated';
    } catch (err) {
      status = err instanceof ApiError ? 'error' : 'error';
    } finally {
      busy = false;
    }
  }

  async function dismiss(): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      await apiPost(`/agents/${slug}/routines/${routine.id}/dismiss`);
      status = 'dismissed';
    } catch {
      status = 'error';
    } finally {
      busy = false;
    }
  }
</script>

{#if status === 'pending'}
  <div
    class="self-start flex flex-col gap-2 px-4 py-3 rounded-xl bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-700/50 text-sm max-w-[90%] md:max-w-[75%]"
  >
    <div class="text-indigo-700 dark:text-indigo-300 font-medium">{shortDesc}</div>
    <div class="text-xs text-indigo-500 dark:text-indigo-400">
      {freq}{timeOfDay}{once}
    </div>
    <label
      class="flex items-center gap-2 text-xs text-indigo-600 dark:text-indigo-400 cursor-pointer"
    >
      <input type="checkbox" bind:checked={notifyEmail} class="accent-indigo-500" />
      Also send via email
    </label>
    <div class="flex gap-2 mt-1">
      <button
        type="button"
        on:click={activate}
        disabled={busy}
        class="bg-indigo-600 hover:bg-indigo-500 transition-colors rounded-lg px-3 py-1 text-xs font-semibold text-white disabled:opacity-60"
      >
        {busy ? 'Activating…' : 'Activate'}
      </button>
      <button
        type="button"
        on:click={dismiss}
        disabled={busy}
        class="bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 transition-colors rounded-lg px-3 py-1 text-xs text-gray-600 dark:text-gray-400"
      >
        Dismiss
      </button>
    </div>
  </div>
{:else if status === 'activated'}
  <div
    class="self-start flex flex-col gap-2 px-4 py-3 rounded-xl bg-green-50 dark:bg-green-900/30 border border-green-300 dark:border-green-700/50 text-sm max-w-[90%] md:max-w-[75%]"
  >
    <span class="text-green-700 dark:text-green-300 text-xs font-medium">✓ Routine activated</span>
  </div>
{:else if status === 'error'}
  <div
    class="self-start flex flex-col gap-2 px-4 py-3 rounded-xl bg-red-50 dark:bg-red-900/30 border border-red-300 dark:border-red-700/50 text-sm max-w-[90%] md:max-w-[75%]"
  >
    <span class="text-red-700 dark:text-red-300 text-xs font-medium">Action failed</span>
  </div>
{/if}
