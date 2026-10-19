<script context="module" lang="ts">
  /**
   * Module-scoped lookup tables — defined once per module load instead
   * of per component instance. Svelte hoists these into the compiled
   * module rather than the component class.
   */
  const FALLBACK_BADGE = 'bg-gray-200 text-gray-700 dark:bg-gray-800 dark:text-gray-300';
  const DECISION_BADGE_CLASSES: Record<string, string> = {
    pending: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
    approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
    rejected: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
    no_improvement: FALLBACK_BADGE,
    error: 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300',
  };
</script>

<script lang="ts">
  /**
   * One improvement-run row — collapsible ``<details>`` summary +
   * rationale / diff / judge-summary / error body. Visual parity with
   * the SSR ``improvements.html`` rows.
   */
  import type { ImprovementRun } from '../../lib/stores/improvements';

  export let run: ImprovementRun;

  $: decisionClass = run.decision ? (DECISION_BADGE_CLASSES[run.decision] ?? FALLBACK_BADGE) : FALLBACK_BADGE;
  $: startedShort = (run.started_at ?? '').slice(0, 19).replace('T', ' ');
  $: deltaPct = run.score_delta !== null && run.score_delta !== undefined
    ? `${run.score_delta >= 0 ? '+' : ''}${(run.score_delta * 100).toFixed(0)}%`
    : null;
  $: shortRationale = run.rationale
    ? run.rationale.length > 120
      ? `${run.rationale.slice(0, 120)}…`
      : run.rationale
    : null;
</script>

<details class="bg-gray-100 dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800">
  <summary class="cursor-pointer px-4 py-3 flex items-center gap-3 flex-wrap text-sm">
    <span class="text-xs font-mono text-gray-500 dark:text-gray-400">{startedShort}</span>
    <span class="text-xs px-2 py-0.5 rounded-full font-semibold {decisionClass}">
      {run.decision ?? '—'}
    </span>
    {#if deltaPct !== null}
      <span class="text-xs text-gray-500 dark:text-gray-400">delta {deltaPct}</span>
    {/if}
    {#if run.model}
      <span class="text-[10px] font-mono text-gray-400 dark:text-gray-500">{run.model}</span>
    {/if}
    {#if run.proposed_sha}
      <span class="text-[10px] font-mono text-indigo-500 dark:text-indigo-400">
        {run.current_sha} → {run.proposed_sha}
      </span>
    {:else}
      <span class="text-[10px] font-mono text-gray-400 dark:text-gray-500">{run.current_sha ?? ''}</span>
    {/if}
    {#if shortRationale}
      <span class="text-xs text-gray-600 dark:text-gray-400 truncate flex-1 min-w-0">
        {shortRationale}
      </span>
    {/if}
  </summary>
  <div class="px-4 pb-4 pt-1 space-y-3 text-sm">
    {#if run.error}
      <div class="bg-rose-50 dark:bg-rose-950/40 border border-rose-200 dark:border-rose-900/50 rounded-lg p-3">
        <p class="text-xs font-semibold text-rose-700 dark:text-rose-400 mb-1">Error</p>
        <pre class="text-xs whitespace-pre-wrap font-mono text-rose-700 dark:text-rose-300">{run.error}</pre>
      </div>
    {/if}
    {#if run.rationale}
      <div>
        <p class="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">Rationale</p>
        <p class="text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap">{run.rationale}</p>
      </div>
    {/if}
    {#if run.judge_summary}
      <div>
        <p class="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">Judge summary</p>
        <p class="text-xs text-gray-600 dark:text-gray-300">{run.judge_summary}</p>
      </div>
    {/if}
    {#if run.proposed_diff}
      <div>
        <p class="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">Diff</p>
        <pre class="text-xs whitespace-pre-wrap font-mono bg-gray-50 dark:bg-gray-950 rounded-lg p-3 overflow-auto max-h-96 border border-gray-200 dark:border-gray-800">{run.proposed_diff}</pre>
      </div>
    {/if}
    <div class="text-[10px] font-mono text-gray-400 dark:text-gray-500">
      run #{run.id} · started {run.started_at}{#if run.finished_at} · finished {run.finished_at}{/if}
    </div>
  </div>
</details>
