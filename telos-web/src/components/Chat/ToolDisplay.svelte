<script lang="ts">
  /**
   * Tool-call panel attached to one assistant turn. Renders the list of
   * in-flight tool rows with a spinner that flips to a checkmark when
   * the matching result arrives. Mirrors
   * `telos/ui/static/js/tool-display.js` — visual parity is the bar.
   *
   * The parent owns the running list of calls via the `rows` prop; this
   * component renders, it doesn't store state. The derivation helpers
   * (`rowFromCall`, `applyResult`) live in `./tool-display.ts` so the
   * orchestrator can import them without instantiating a component.
   */
  import type { ToolRow } from './tool-display';

  export let rows: ToolRow[] = [];

  let collapsed = false;
  function toggle(): void {
    collapsed = !collapsed;
  }

  $: pending = rows.filter((r) => r.status === 'pending').length;
  $: doneLabel = `${rows.length} tool call${rows.length === 1 ? '' : 's'}`;
</script>

{#if rows.length > 0}
  <div class="self-start w-full max-w-[90%] md:max-w-[75%]">
    <div class="rounded-xl border border-gray-700/60 bg-gray-950 overflow-hidden text-xs font-mono">
      <button
        type="button"
        class="w-full flex items-center justify-between px-3 py-2 text-gray-500 hover:text-gray-300 transition-colors select-none"
        on:click={toggle}
      >
        <span class="flex items-center gap-2">
          {#if pending > 0}
            <span class="spin text-gray-600">↻</span>
            <span class="text-gray-400">running tools…</span>
          {:else}
            <span class="text-green-500">✓</span>
            <span class="text-gray-400">{doneLabel}</span>
          {/if}
        </span>
        <span class="text-gray-600 text-[10px]">{collapsed ? '▸' : '▾'}</span>
      </button>
      {#if !collapsed}
        <div class="border-t border-gray-800 overflow-y-auto" style="max-height:160px">
          {#each rows as row (row.key)}
            <div
              class="flex items-start gap-2.5 px-3 py-2 border-b border-gray-800/60 last:border-0"
            >
              <span
                class="mt-px shrink-0 {row.status === 'pending'
                  ? 'spin text-gray-500'
                  : row.status === 'error'
                    ? 'text-red-400'
                    : 'text-green-400'}"
                style="font-size:12px"
              >
                {row.status === 'pending' ? '↻' : row.status === 'error' ? '✗' : '✓'}
              </span>
              <span
                class="shrink-0 rounded px-1.5 py-px text-[10px] tracking-wide uppercase bg-gray-800 text-indigo-300"
              >
                {row.badge}
              </span>
              <span class="text-gray-500 truncate leading-snug">{row.preview}</span>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>
{/if}
