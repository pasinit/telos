<script lang="ts">
  /**
   * Three-stat grid (follow-up / correction / turns) showing the
   * agent's quality signals at a glance. Mirrors the SSR widget.
   */
  import type { SignalSummary } from './types';

  export let summary: SignalSummary;

  $: followUpPct = Math.round((summary.follow_up_rate ?? 0) * 100);
  $: correctionPct = Math.round((summary.correction_rate ?? 0) * 100);
</script>

<div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-gray-200 dark:border-gray-800 space-y-3">
  <h2 class="font-semibold text-sm text-gray-600 dark:text-gray-300 uppercase tracking-wide">Quality signals</h2>
  <div class="grid grid-cols-3 gap-4 text-center">
    <div>
      <p class="text-2xl font-bold text-green-400">{followUpPct}%</p>
      <p class="text-xs text-gray-500 mt-0.5">follow-up rate</p>
    </div>
    <div>
      <p class="text-2xl font-bold text-red-400">{correctionPct}%</p>
      <p class="text-xs text-gray-500 mt-0.5">correction rate</p>
    </div>
    <div>
      <p class="text-2xl font-bold text-gray-400">{summary.total_turns}</p>
      <p class="text-xs text-gray-500 mt-0.5">total turns</p>
    </div>
  </div>
</div>
