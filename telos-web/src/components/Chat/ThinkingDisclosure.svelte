<script lang="ts">
  /**
   * Collapsible extended-thinking disclosure (Claude.ai / o1 style),
   * rendered as a SEPARATE element ABOVE the assistant answer balloon —
   * never inside it, so the balloon never resizes due to thinking.
   *
   * Two phases, driven by props from `Chat.svelte`:
   * - Streaming (`durationSec == null`): a small "✶ Thinking…" row with a
   *   subtle pulse. The answer balloon does not exist yet.
   * - Collapsed (`durationSec != null`): a compact "› Thought for {N}s"
   *   chip. Clicking it toggles a muted, scrollable, read-only panel with
   *   the FULL accumulated thinking text. Collapsed by default.
   *
   * The thinking text is purely transient (in-memory for the session);
   * see `thinking.ts`. This component renders it as plain, monospace,
   * muted text — visually distinct from the rendered-markdown answer.
   */

  /** FULL accumulated thinking text (for the expand panel). */
  export let text: string = '';
  /**
   * Rounded-seconds duration. `null` while thinking still streams (shows
   * the "Thinking…" row); a number collapses it into the chip.
   */
  export let durationSec: number | null = null;

  let open = false;

  $: streaming = durationSec === null;

  function toggle(): void {
    open = !open;
  }
</script>

{#if streaming}
  <!-- Pre-text phase: own-row activity indicator, NOT in the balloon. -->
  <div
    class="self-start flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400 animate-pulse"
    aria-live="polite"
  >
    <span class="spin">✶</span>
    <span>Thinking…</span>
  </div>
{:else}
  <div class="self-start flex flex-col gap-1 max-w-[90%] md:max-w-[60%]">
    <button
      type="button"
      on:click={toggle}
      aria-expanded={open}
      class="inline-flex items-center gap-1 px-2 py-0.5 w-fit rounded-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
    >
      <span class="inline-block transition-transform" class:rotate-90={open}>›</span>
      <span>Thought for {durationSec}s</span>
    </button>
    {#if open}
      <pre
        class="mt-0.5 max-h-60 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 dark:bg-gray-900/60 border border-gray-200 dark:border-gray-800 px-3 py-2 text-[11px] leading-relaxed font-mono text-gray-500 dark:text-gray-400">{text}</pre>
    {/if}
  </div>
{/if}
