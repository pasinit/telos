<script lang="ts">
  /**
   * Per-prompt history table with a rollback button for non-active
   * entries. Mirrors the SSR widget; the parent handles the actual
   * rollback POST (this component just reports the chosen sha).
   */
  import { link } from 'svelte-spa-router';
  import type { PromptHistoryEntry } from './types';

  export let entries: PromptHistoryEntry[];
  export let slug: string;
  export let onRollback: (sha: string) => void;
  export let submitting: boolean = false;

  function isActive(p: PromptHistoryEntry): boolean {
    return Boolean(p.deployed_at) && !p.retired_at;
  }

  function isRollbackable(p: PromptHistoryEntry): boolean {
    return Boolean(p.retired_at) || !p.deployed_at;
  }
</script>

{#if entries.length > 0}
  <div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-gray-200 dark:border-gray-800 space-y-3">
    <div class="flex items-center justify-between">
      <h2 class="font-semibold text-sm text-gray-600 dark:text-gray-300 uppercase tracking-wide">Prompt history</h2>
      <a href="/agents/{slug}/improvements" use:link class="text-xs text-indigo-500 hover:text-indigo-400 transition-colors">Improvement runs →</a>
    </div>
    <div class="space-y-2">
      {#each entries as p (p.sha)}
        <div class="flex items-center justify-between py-2 border-b border-gray-800 last:border-0 text-sm">
          <div class="flex items-center gap-3">
            <code class="text-indigo-400 font-mono text-xs">{p.sha}</code>
            <span class="text-gray-500 text-xs">{p.created_at.slice(0, 10)}</span>
            {#if isActive(p)}
              <span class="bg-green-900/50 text-green-400 text-xs px-2 py-0.5 rounded-full">active</span>
            {/if}
          </div>
          {#if isRollbackable(p)}
            <button
              class="text-xs text-gray-500 hover:text-gray-300 transition-colors disabled:opacity-50"
              disabled={submitting}
              on:click={() => onRollback(p.sha)}
            >
              rollback
            </button>
          {/if}
        </div>
      {/each}
    </div>
  </div>
{:else}
  <div class="text-xs text-gray-500 dark:text-gray-400">
    <a href="/agents/{slug}/improvements" use:link class="text-indigo-500 hover:text-indigo-400 transition-colors">View improvement runs →</a>
  </div>
{/if}
