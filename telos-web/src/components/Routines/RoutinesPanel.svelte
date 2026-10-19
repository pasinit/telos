<script lang="ts">
  /**
   * Routines management panel for the agent settings page.
   *
   * Lists active routines and exposes inline edits (frequency, time of
   * day, email notification) plus delete. Pending proposals (rows the
   * agent staged with enabled=false) appear too, with Activate /
   * Dismiss — the chat pill is the primary confirmation surface, but
   * proposals from past turns (including orphans staged while the pill
   * was broken) are only reachable here.
   *
   * Mutations call the existing JSON endpoints:
   * ``PATCH/DELETE /agents/{slug}/routines/{id}`` and
   * ``POST /agents/{slug}/routines/{id}/{activate,dismiss}``.
   * Optimistic UI: state updates locally on success and rolls back on
   * error.
   */
  import { api, apiDelete, apiPost } from '../../lib/api';
  import type { RoutineRow } from '../Settings/types';

  export let slug: string;
  export let routines: RoutineRow[];

  // Local mirror so optimistic edits don't fight the parent's payload.
  // The parent only refreshes after Save Tools / Approve / Reject —
  // routine edits are inline-only.
  let rows: RoutineRow[] = routines;
  $: rows = routines;

  let busy = new Set<string>();
  let error: string | null = null;

  const INTERVAL_PRESETS: { value: number; label: string }[] = [
    { value: 300, label: '5 min' },
    { value: 1800, label: '30 min' },
    { value: 3600, label: '1 hour' },
    { value: 21600, label: '6 hours' },
    { value: 43200, label: '12 hours' },
    { value: 86400, label: 'Daily' },
    { value: 604800, label: 'Weekly' },
  ];

  function presetOptions(current: number): { value: number; label: string }[] {
    if (INTERVAL_PRESETS.some((p) => p.value === current)) return INTERVAL_PRESETS;
    // Surface the non-preset value so the select doesn't silently snap
    // the routine to the nearest preset on first change.
    return [...INTERVAL_PRESETS, { value: current, label: `Custom (${current}s)` }];
  }

  async function patchRoutine(id: string, body: Record<string, unknown>): Promise<void> {
    busy = new Set([...busy, id]);
    error = null;
    try {
      await api(`/agents/${slug}/routines/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      // Optimistic local update — server returns {ok: true} only.
      rows = rows.map((r) => (r.id === id ? { ...r, ...body } : r));
    } catch (e) {
      error = `Update failed for routine ${id.slice(0, 8)}.`;
    } finally {
      busy = new Set([...busy].filter((b) => b !== id));
    }
  }

  async function onInterval(id: string, event: Event): Promise<void> {
    const value = parseInt((event.target as HTMLSelectElement).value, 10);
    if (!Number.isFinite(value)) return;
    await patchRoutine(id, { interval_seconds: value });
  }

  async function onTimeOfDay(id: string, event: Event): Promise<void> {
    const value = (event.target as HTMLInputElement).value;
    // Empty string clears the time-of-day; send `null` so the backend
    // interprets it as "no anchor".
    await patchRoutine(id, { time_of_day: value || null });
  }

  async function onNotifyEmail(id: string, event: Event): Promise<void> {
    const value = (event.target as HTMLInputElement).checked;
    await patchRoutine(id, { notify_email: value });
  }

  async function onDelete(id: string): Promise<void> {
    if (!window.confirm('Delete this routine?')) return;
    busy = new Set([...busy, id]);
    error = null;
    try {
      await apiDelete(`/agents/${slug}/routines/${id}`);
      rows = rows.filter((r) => r.id !== id);
    } catch (e) {
      error = `Delete failed for routine ${id.slice(0, 8)}.`;
    } finally {
      busy = new Set([...busy].filter((b) => b !== id));
    }
  }

  async function onActivate(id: string): Promise<void> {
    busy = new Set([...busy, id]);
    error = null;
    try {
      // notify_email follows whatever the row already says — the inline
      // checkbox edits it independently of activation.
      const row = rows.find((r) => r.id === id);
      await apiPost(`/agents/${slug}/routines/${id}/activate`, {
        notify_email: row?.notify_email ?? false,
      });
      rows = rows.map((r) => (r.id === id ? { ...r, enabled: true } : r));
    } catch (e) {
      error = `Activate failed for routine ${id.slice(0, 8)}.`;
    } finally {
      busy = new Set([...busy].filter((b) => b !== id));
    }
  }

  async function onDismiss(id: string): Promise<void> {
    busy = new Set([...busy, id]);
    error = null;
    try {
      // Dismiss (not delete) for pending proposals: the endpoint also
      // tells the agent not to re-propose the routine unprompted.
      await apiPost(`/agents/${slug}/routines/${id}/dismiss`);
      rows = rows.filter((r) => r.id !== id);
    } catch (e) {
      error = `Dismiss failed for routine ${id.slice(0, 8)}.`;
    } finally {
      busy = new Set([...busy].filter((b) => b !== id));
    }
  }

  function displayName(r: RoutineRow): string {
    return r.name || r.description.split('\n')[0].slice(0, 60);
  }
</script>

<div
  class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-gray-200 dark:border-gray-800 space-y-3"
>
  <h2
    class="font-semibold text-sm text-gray-600 dark:text-gray-300 uppercase tracking-wide"
  >
    Scheduled routines
  </h2>

  {#if error}
    <p class="text-xs text-red-500">{error}</p>
  {/if}

  {#if rows.length === 0}
    <p class="text-xs text-gray-600 dark:text-gray-400">
      No routines set up. Ask the agent to create one in chat.
    </p>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr
            class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide border-b border-gray-200 dark:border-gray-800"
          >
            <th class="text-left py-2 px-2 font-medium">Routine</th>
            <th class="text-left py-2 px-2 font-medium">Frequency</th>
            <th class="text-left py-2 px-2 font-medium">Time</th>
            <th class="text-center py-2 px-2 font-medium">Email</th>
            <th class="text-left py-2 px-2 font-medium">Next run</th>
            <th class="py-2 px-2"></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as r (r.id)}
            <tr
              class="border-b border-gray-100 dark:border-gray-800/50 last:border-0"
              class:opacity-50={busy.has(r.id)}
            >
              <td class="py-3 px-2 max-w-[200px]">
                <p class="text-gray-800 dark:text-gray-200 truncate" title={r.description}>
                  {displayName(r)}
                </p>
              </td>
              <td class="py-3 px-2">
                <select
                  value={r.interval_seconds}
                  on:change={(e) => onInterval(r.id, e)}
                  disabled={busy.has(r.id)}
                  class="bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded px-2 py-1 text-xs"
                >
                  {#each presetOptions(r.interval_seconds) as opt (opt.value)}
                    <option value={opt.value}>{opt.label}</option>
                  {/each}
                </select>
              </td>
              <td class="py-3 px-2">
                <input
                  type="time"
                  value={r.time_of_day ?? ''}
                  on:change={(e) => onTimeOfDay(r.id, e)}
                  disabled={busy.has(r.id)}
                  class="bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded px-2 py-1 text-xs w-24"
                />
              </td>
              <td class="py-3 px-2 text-center">
                <input
                  type="checkbox"
                  checked={r.notify_email}
                  on:change={(e) => onNotifyEmail(r.id, e)}
                  disabled={busy.has(r.id)}
                  class="accent-indigo-500"
                />
              </td>
              <td class="py-3 px-2">
                {#if r.next_run}
                  <span class="text-xs text-gray-600 dark:text-gray-300">{r.next_run}</span>
                {:else if r.enabled}
                  <span class="text-xs text-green-600 dark:text-green-400">active</span>
                {:else}
                  <span class="text-xs text-gray-400">pending</span>
                {/if}
              </td>
              <td class="py-3 px-2">
                {#if r.enabled}
                  <button
                    type="button"
                    on:click={() => onDelete(r.id)}
                    disabled={busy.has(r.id)}
                    class="text-xs text-gray-400 hover:text-red-400 transition-colors disabled:opacity-50"
                  >
                    Delete
                  </button>
                {:else}
                  <div class="flex gap-2 justify-end">
                    <button
                      type="button"
                      on:click={() => onActivate(r.id)}
                      disabled={busy.has(r.id)}
                      class="text-xs font-semibold text-indigo-600 dark:text-indigo-400 hover:text-indigo-500 transition-colors disabled:opacity-50"
                    >
                      Activate
                    </button>
                    <button
                      type="button"
                      on:click={() => onDismiss(r.id)}
                      disabled={busy.has(r.id)}
                      class="text-xs text-gray-400 hover:text-red-400 transition-colors disabled:opacity-50"
                    >
                      Dismiss
                    </button>
                  </div>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>
