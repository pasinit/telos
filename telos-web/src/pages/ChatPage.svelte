<script lang="ts">
  /**
   * Specialist chat page (`/agents/:slug`). Thin wrapper around the
   * shared `<Chat>` component bound to the URL parameter.
   *
   * Composes inside `<Shell>` with:
   * - `sidebarFooter` slot → Settings link + Clear-history button
   *   (Chat exposes a public `clearHistory()` method via `bind:this`).
   * - `mobileHeader` slot → agent name (Chat exposes via `bind:agentName`).
   *
   * `{#key slug}` rebuilds Chat from scratch when the route param
   * changes (e.g. navigating from one agent to another via the
   * sidebar) — that's the right behaviour: each agent has its own
   * AbortController, usage state, and history.
   */
  import { params, link } from 'svelte-spa-router';
  import Shell from '../components/Shell.svelte';
  import Chat from '../components/Chat/Chat.svelte';

  $: slug = ($params as Record<string, string> | undefined)?.slug ?? '';

  // Bound from Chat after meta hydration.
  let agentName = '';
  let chatRef: Chat | undefined;

  // Two-click confirmation for the destructive clear-history action.
  // 4-second window matches the previous SSR `confirm('Clear all
  // conversation history?')` pattern; auto-resets after the timeout.
  let confirmingClear = false;
  let confirmTimer: ReturnType<typeof setTimeout> | null = null;

  async function onClearClick(): Promise<void> {
    if (!confirmingClear) {
      confirmingClear = true;
      if (confirmTimer) clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => {
        confirmingClear = false;
        confirmTimer = null;
      }, 4000);
      return;
    }
    if (confirmTimer) {
      clearTimeout(confirmTimer);
      confirmTimer = null;
    }
    confirmingClear = false;
    await chatRef?.clearHistory();
  }
</script>

<Shell activeAgentSlug={slug}>
  <svelte:fragment slot="sidebarFooter">
    {#if slug}
      <a
        href={`/agents/${slug}/settings`}
        use:link
        class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
        >⚙ Settings</a
      >
      <button
        type="button"
        on:click={onClearClick}
        class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-sm text-gray-500 dark:text-gray-400 hover:text-red-500 w-full text-left"
        title="Clear conversation history"
      >
        {confirmingClear ? '✕ Click again to confirm' : '✕ Clear history'}
      </button>
    {/if}
  </svelte:fragment>

  <svelte:fragment slot="mobileHeader">
    {#if agentName}
      <h1 class="font-semibold text-gray-800 dark:text-gray-100 truncate">{agentName}</h1>
    {/if}
  </svelte:fragment>

  {#if slug}
    {#key slug}
      <Chat {slug} bind:agentName bind:this={chatRef} />
    {/key}
  {/if}
</Shell>
