<script lang="ts">
  /**
   * One night's per-agent feedback: score, summary, and the analyst's
   * specifics (failures, classified recall misses, prompt suggestions,
   * tool-use issues). A failed analysis row shows its error instead.
   */
  import type { NightlyFeedbackRow } from '../../lib/stores/nightly';

  export let row: NightlyFeedbackRow;

  $: fb = row.feedback;

  function scoreClass(score: number): string {
    if (score <= 2) return 'bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300';
    if (score === 3) return 'bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300';
    return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300';
  }

  const GAP_LABEL: Record<string, string> = {
    extraction_gap: 'extraction',
    ranking_gap: 'ranking',
    budget_gap: 'budget',
    usage_gap: 'usage',
  };
  function gapClass(cls: string): string {
    // usage_gap is the agent's fault; the others are memory's — tint accordingly.
    return cls === 'usage_gap'
      ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-950/50 dark:text-indigo-300'
      : 'bg-sky-100 text-sky-700 dark:bg-sky-950/50 dark:text-sky-300';
  }
</script>

<div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-5 border border-gray-200 dark:border-gray-800 space-y-3">
  {#if row.status !== 'completed' || !fb}
    <div class="flex items-center gap-2">
      <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300">
        {row.status}
      </span>
      <span class="text-xs text-gray-500">run #{row.run_id}</span>
    </div>
    <p class="text-sm text-gray-600 dark:text-gray-400">{row.error ?? 'No feedback produced.'}</p>
  {:else}
    <div class="flex items-center gap-2">
      <span class="text-sm font-bold px-2.5 py-0.5 rounded-full {scoreClass(fb.score)}">{fb.score}/5</span>
      <span class="text-xs text-gray-500">run #{row.run_id}</span>
    </div>
    <p class="text-sm text-gray-800 dark:text-gray-200">{fb.summary}</p>

    {#if fb.failures.length}
      <div>
        <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Issues</h4>
        <ul class="space-y-1">
          {#each fb.failures as f (f.turn_id + f.issue)}
            <li class="text-sm text-gray-700 dark:text-gray-300">
              <span class="font-mono text-xs text-gray-400">{f.turn_id.slice(0, 8)}</span> — {f.issue}
              {#if f.evidence}<span class="text-gray-500 dark:text-gray-500"> ({f.evidence})</span>{/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if fb.recall_misses.length}
      <div>
        <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Recall misses</h4>
        <ul class="space-y-1">
          {#each fb.recall_misses as m (m.turn_id + m.query_used)}
            <li class="text-sm text-gray-700 dark:text-gray-300 flex items-start gap-2">
              <span class="text-[0.65rem] font-semibold px-1.5 py-0.5 rounded {gapClass(m.classification)} shrink-0 mt-0.5">
                {GAP_LABEL[m.classification] ?? m.classification}
              </span>
              <span>“{m.query_used}”{#if m.detail} — {m.detail}{/if}</span>
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if fb.prompt_suggestions.length}
      <div>
        <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Prompt suggestions</h4>
        <ul class="list-disc list-inside space-y-0.5">
          {#each fb.prompt_suggestions as s, i (i)}
            <li class="text-sm text-gray-700 dark:text-gray-300">{s}</li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if fb.tool_use_issues.length}
      <div>
        <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Tool use</h4>
        <ul class="list-disc list-inside space-y-0.5">
          {#each fb.tool_use_issues as t, i (i)}
            <li class="text-sm text-gray-700 dark:text-gray-300">{t}</li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}
</div>
