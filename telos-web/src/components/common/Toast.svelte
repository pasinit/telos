<script lang="ts">
  /**
   * Toast queue renderer.
   *
   * Mounted once globally in `App.svelte`. Subscribes to the `toasts`
   * store and renders the live list bottom-right (top on mobile). Each
   * toast self-dismisses after its `timeoutMs`; users can also click
   * the × to dismiss early.
   *
   * Styling: Tailwind only (no entries in `app.css`), so the toast
   * affordance ships standalone.
   */
  import { onDestroy } from 'svelte';
  import { toasts, dismissToast, type Toast } from '../../lib/toast';

  // Track the setTimeout handle per toast id so we can cancel them on
  // unmount (avoids "dismissing a toast that's already gone" no-ops on
  // late timers in HMR).
  const _timers = new Map<number, ReturnType<typeof setTimeout>>();

  $: scheduleTimers($toasts);
  function scheduleTimers(list: Toast[]): void {
    for (const t of list) {
      if (_timers.has(t.id)) continue;
      if (t.timeoutMs > 0) {
        const handle = setTimeout(() => dismissToast(t.id), t.timeoutMs);
        _timers.set(t.id, handle);
      }
    }
    // Drop timers whose toasts have been dismissed.
    const live = new Set(list.map((t) => t.id));
    for (const id of [..._timers.keys()]) {
      if (!live.has(id)) {
        clearTimeout(_timers.get(id)!);
        _timers.delete(id);
      }
    }
  }

  onDestroy(() => {
    for (const h of _timers.values()) clearTimeout(h);
    _timers.clear();
  });

  function kindClasses(kind: Toast['kind']): string {
    switch (kind) {
      case 'success':
        return 'bg-green-600 text-white';
      case 'error':
        return 'bg-red-600 text-white';
      default:
        return 'bg-gray-800 text-white dark:bg-gray-700';
    }
  }
</script>

<div
  class="fixed z-50 flex flex-col gap-2 pointer-events-none right-4 bottom-4 max-w-sm"
  aria-live="polite"
  aria-atomic="false"
>
  {#each $toasts as toast (toast.id)}
    <div
      role="status"
      class="pointer-events-auto flex items-start gap-3 rounded-lg px-4 py-3 shadow-lg text-sm {kindClasses(toast.kind)}"
    >
      <span class="flex-1">{toast.message}</span>
      <button
        type="button"
        class="opacity-70 hover:opacity-100 transition-opacity"
        on:click={() => dismissToast(toast.id)}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  {/each}
</div>
