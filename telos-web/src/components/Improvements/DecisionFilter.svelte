<script lang="ts">
  /**
   * Decision filter dropdown. Mirrors the SSR ``<select>`` in
   * ``improvements.html`` — same options, same "All" empty value.
   * Calling-side passes the bound value + a callback so the page can
   * forward the change to the store.
   */
  import { DECISIONS, type Decision } from '../../lib/stores/improvements';

  export let value: Decision | null = null;
  export let runCount: number = 0;
  export let onChange: (next: Decision | null) => void = () => {};

  function handleChange(event: Event): void {
    const target = event.currentTarget as HTMLSelectElement;
    const v = target.value;
    onChange(v === '' ? null : (v as Decision));
  }
</script>

<div class="flex flex-wrap items-center gap-3 text-sm">
  <label class="text-gray-500 dark:text-gray-400" for="decision-filter">Decision</label>
  <select
    id="decision-filter"
    name="decision"
    on:change={handleChange}
    value={value ?? ''}
    class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded px-2 py-1 text-xs"
  >
    <option value="">All</option>
    {#each DECISIONS as d}
      <option value={d}>{d}</option>
    {/each}
  </select>
  <span class="text-xs text-gray-500">{runCount} run{runCount === 1 ? '' : 's'}</span>
</div>
