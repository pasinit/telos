<script lang="ts">
  /**
   * Read-only view of the current deployed system prompt.
   *
   * Editing the prompt directly is intentionally NOT exposed in this
   * version — prompt changes flow through the improver pipeline so the
   * audit trail (``prompt_history`` + ``improvement_runs``) stays
   * complete. Matches the SSR template.
   */
  export let prompt: string;
</script>

<div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 space-y-3 border border-gray-200 dark:border-gray-800">
  <h2 class="font-semibold text-sm text-gray-600 dark:text-gray-300 uppercase tracking-wide">Current prompt</h2>
  <pre class="text-sm text-gray-300 whitespace-pre-wrap font-mono bg-gray-50 dark:bg-gray-950 rounded-xl p-4 overflow-auto max-h-60">{prompt}</pre>
</div>
