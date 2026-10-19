<script lang="ts">
  /**
   * Agent deletion. Confirms via ``window.confirm`` to match the SSR
   * behaviour — we don't have a generic Modal yet (planned for a later
   * PR), so this stays as the minimum surface that doesn't regress UX.
   */
  export let onDelete: () => void;
  export let submitting: boolean = false;
</script>

<div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-red-900/40 space-y-3">
  <h2 class="font-semibold text-sm text-red-400 uppercase tracking-wide">Danger zone</h2>
  <div class="flex items-center justify-between">
    <div>
      <p class="text-sm font-medium">Delete agent</p>
      <p class="text-xs text-gray-500">Permanently removes this agent and all its history.</p>
    </div>
    <button
      class="bg-red-900/50 hover:bg-red-800/70 text-red-400 transition-colors rounded-lg px-4 py-2 text-sm disabled:opacity-50"
      disabled={submitting}
      on:click={onDelete}
    >
      Delete
    </button>
  </div>
</div>
