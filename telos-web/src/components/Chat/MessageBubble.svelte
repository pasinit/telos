<script lang="ts">
  /**
   * One assistant or user message bubble plus its supporting chrome:
   * tool-call panel, pending routine/agent pills, and a "View recalled
   * memories" link below the bubble once the message has finished
   * streaming (the recalled panel is only meaningful after the turn).
   *
   * Visual parity with `chat.html` is the bar — see CSS in `app.css`
   * (`.agent-response`, `.cursor`).
   */
  import ToolDisplay from './ToolDisplay.svelte';
  import type { ToolRow } from './tool-display';
  import RoutinePill from './RoutinePill.svelte';
  import AgentPill from './AgentPill.svelte';
  import { render } from '../../lib/markdown';
  import type { PendingRoutineEvent, PendingAgentEvent } from './stream';

  export let role: 'user' | 'assistant';
  export let raw: string = '';
  /** Server-rendered HTML for stored turns (back-compat). */
  export let html: string | null = null;
  export let timestamp: string = '';
  export let attachedFilename: string | null = null;
  export let isStreaming: boolean = false;
  export let isEmpty: boolean = false;
  /** Routine label for routine-rendered turns (user side). */
  export let routineLabel: string | null = null;
  export let routineDescription: string | null = null;

  /** Tool panel rows. */
  export let toolRows: ToolRow[] = [];
  /** Pending routine / agent proposals attached to this turn. */
  export let pendingRoutines: PendingRoutineEvent[] = [];
  export let pendingAgents: PendingAgentEvent[] = [];
  /** Slug owning the agent (for routine API calls). */
  export let slug: string = '';
  /** Turn ID for the "view recalled" link; null when not yet known. */
  export let turnId: string | null = null;
  /** Whether a briefing fired this turn (drives recalled-link presence). */
  export let hasBriefing: boolean = false;
  /** Cost in USD for this turn's chat call. Rendered as a small badge below
   *  the assistant message. ``null`` while a live turn is streaming. */
  export let turnCost: number | null = null;
  /** Callbacks. */
  export let onOpenRecalled: ((turnId: string) => void) | undefined = undefined;

  // Pre-rendered HTML wins (stored history); otherwise render the
  // streaming raw markdown via the shared marked wrapper.
  $: bodyHtml = html ?? render(raw);
  $: showCursor = role === 'assistant' && isStreaming;
  // Post-completion only: while streaming, no recalled-memories
  // affordance renders at all (the briefing fires before the answer but
  // the panel content is only meaningful once the turn is done).
  $: showRecalledLink =
    role === 'assistant' && !isStreaming && hasBriefing && turnId && onOpenRecalled;

  function clickRecalled(): void {
    if (turnId && onOpenRecalled) onOpenRecalled(turnId);
  }
</script>

<div class="flex flex-col gap-1">
  {#if role === 'user' && routineLabel}
    <div class="my-2 flex items-center gap-3 text-[11px] text-indigo-700 dark:text-indigo-300">
      <div class="flex-1 h-px bg-indigo-300/40 dark:bg-indigo-700/40"></div>
      <div
        class="flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-700/50"
        title={routineDescription ?? ''}
      >
        <span>⏰</span>
        <span class="font-medium">Routine</span>
        <span class="text-indigo-600/80 dark:text-indigo-400/80">·</span>
        <span class="truncate max-w-[260px] md:max-w-[420px]">{routineLabel}</span>
        {#if timestamp}
          <span class="text-indigo-600/60 dark:text-indigo-400/60">·</span>
          <span class="text-indigo-600/70 dark:text-indigo-400/70">{timestamp}</span>
        {/if}
      </div>
      <div class="flex-1 h-px bg-indigo-300/40 dark:bg-indigo-700/40"></div>
    </div>
  {:else if role === 'user'}
    <div
      class="self-end max-w-[90%] md:max-w-[60%] bg-brand text-white rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm whitespace-pre-wrap"
    >
      {raw}
    </div>
    {#if attachedFilename}
      <div
        class="self-end flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-indigo-900/50 border border-indigo-700/50 text-xs text-indigo-300"
      >
        <span>📄</span>
        <span class="truncate max-w-[200px]">{attachedFilename}</span>
      </div>
    {/if}
    {#if timestamp}
      <span class="self-end text-[10px] text-gray-600">{timestamp}</span>
    {/if}
  {/if}

  {#if role === 'assistant'}
    <ToolDisplay rows={toolRows} />
    <div
      class="self-start max-w-[90%] md:max-w-[60%] bg-gray-100 dark:bg-gray-800 rounded-2xl rounded-tl-sm px-4 py-2.5 text-sm text-gray-700 dark:text-gray-200 agent-response {showCursor
        ? 'cursor'
        : ''}"
      data-turn-id={turnId ?? ''}
    >
      {#if isEmpty && !showCursor}
        <span class="text-gray-600 italic">No response.</span>
      {:else}
        {@html bodyHtml}
      {/if}
    </div>

    {#each pendingRoutines as r (r.id)}
      <RoutinePill {slug} routine={r} />
    {/each}
    {#each pendingAgents as a, i (i)}
      <AgentPill agent={a} />
    {/each}

    {#if showRecalledLink}
      <button
        type="button"
        on:click={clickRecalled}
        class="self-start text-[10px] text-indigo-500 hover:text-indigo-400 transition-colors flex items-center gap-1"
      >
        <span>◈</span> View recalled memories
      </button>
    {/if}
    {#if timestamp || (turnCost != null && turnCost > 0)}
      <span class="self-start text-[10px] text-gray-600">
        {timestamp}
        {#if turnCost != null && turnCost > 0}
          {timestamp ? '·' : ''} ${turnCost.toFixed(4)}
        {/if}
      </span>
    {/if}
  {/if}
</div>
