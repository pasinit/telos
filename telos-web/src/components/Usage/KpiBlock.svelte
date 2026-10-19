<script lang="ts">
  /**
   * Headline KPI block: total spend $ + N calls + avg/call.
   *
   * Mirrors the SSR `<section>` at the top of `usage.html`. All numbers
   * come pre-computed from `lib/usage.ts` so the component is purely
   * declarative.
   */
  import { formatUsd, pluraliseCalls, type RangeToken } from '../../lib/usage';

  export let totalUsd: number;
  export let totalCalls: number;
  export let avgCost: number;
  export let range: RangeToken;

  $: rangeLabel = range === 'all' ? '(all-time)' : `(last ${range})`;
</script>

<section class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 md:p-8 border border-gray-200 dark:border-gray-800">
  <p class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
    Total spend {rangeLabel}
  </p>
  <p class="text-4xl md:text-5xl font-bold tabular-nums">{formatUsd(totalUsd, 2)}</p>
  <p class="text-sm text-gray-500 dark:text-gray-400 mt-3">
    {pluraliseCalls(totalCalls)} &middot; avg {formatUsd(avgCost, 4)}/call
  </p>
</section>
