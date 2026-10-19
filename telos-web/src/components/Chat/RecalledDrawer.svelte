<script lang="ts">
  /**
   * Right-edge drawer that displays the memories Recordatio recalled for
   * one turn. Triggered by the briefing recordatio chip and the
   * "View recalled memories" link below each assistant bubble.
   *
   * Ports `telos/ui/static/js/recalled-drawer.js`. Visual parity is the
   * bar — the styling lives in `app.css` (`.recalled-drawer-backdrop`,
   * `.recalled-drawer`, `.recalled-section`, `.recalled-item`,
   * `.recalled-type-badge`).
   */
  import { onDestroy, onMount } from 'svelte';
  import { apiGet, ApiError } from '../../lib/api';

  export let open = false;
  export let slug: string = '';
  export let turnId: string = '';
  export let onClose: () => void = () => {};

  interface RecalledPayload {
    recalled?: null;
    reason?: string;
    query_text?: string;
    topic_hint?: string;
    units?: Array<{ type?: string; content?: string }>;
    entities?: Array<{ label?: string; description?: string }>;
    relations?: Array<{ subject?: string; predicate?: string; object?: string }>;
    recordatio_public_url?: string;
    agent_id?: string;
  }

  let payload: RecalledPayload | null = null;
  let loading = false;
  let error: string | null = null;

  // Re-fetch whenever the (open, slug, turnId) triplet changes to a
  // valid "open this turn" combination.
  $: if (open && slug && turnId) {
    loadPayload(slug, turnId);
  }

  async function loadPayload(s: string, t: string): Promise<void> {
    loading = true;
    error = null;
    payload = null;
    try {
      payload = await apiGet<RecalledPayload>(`/agents/${encodeURIComponent(s)}/turns/${encodeURIComponent(t)}/memories`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        error =
          'No recalled memories are available for this turn (the briefing event was not retained).';
      } else if (err instanceof Error) {
        error = `Failed to load: ${err.message}`;
      } else {
        error = 'Failed to load.';
      }
    } finally {
      loading = false;
    }
  }

  function onKey(e: KeyboardEvent): void {
    if (e.key === 'Escape' && open) onClose();
  }

  onMount(() => {
    document.addEventListener('keydown', onKey);
  });
  onDestroy(() => {
    document.removeEventListener('keydown', onKey);
  });

  function externalUrl(p: RecalledPayload): string | null {
    if (p.recordatio_public_url && p.agent_id) {
      return `${p.recordatio_public_url}/memories?agent_id=${encodeURIComponent(p.agent_id)}`;
    }
    return null;
  }
</script>

{#if open}
  <div
    class="recalled-drawer-backdrop"
    on:click={onClose}
    role="presentation"
  >
    <div class="recalled-drawer" on:click|stopPropagation role="presentation">
      <div class="flex items-center justify-between px-5 py-3 border-b border-gray-200 dark:border-gray-800">
        <h2 class="text-sm font-semibold text-gray-700 dark:text-gray-200">Recalled memories</h2>
        <button
          type="button"
          on:click={onClose}
          class="text-gray-500 hover:text-gray-300 text-lg leading-none"
          aria-label="Close drawer">&times;</button
        >
      </div>
      <div class="overflow-y-auto px-5 py-4 text-sm text-gray-700 dark:text-gray-200">
        {#if loading}
          <div class="text-gray-500 italic">Loading…</div>
        {:else if error}
          <div class="text-red-400">{error}</div>
        {:else if payload}
          {#if payload.recalled === null}
            <div class="text-gray-500 italic">
              No recalled memories were recorded for this turn.
            </div>
          {:else}
            {#if payload.query_text || payload.topic_hint}
              <div class="recalled-section">
                {#if payload.query_text}
                  <h3>Query</h3>
                  <div class="text-gray-600 dark:text-gray-300">{payload.query_text}</div>
                {/if}
                {#if payload.topic_hint}
                  <div class="mt-2 text-xs text-gray-500">
                    <span class="uppercase tracking-wider">Topic</span> · {payload.topic_hint}
                  </div>
                {/if}
              </div>
            {/if}
            {#if payload.units && payload.units.length}
              <div class="recalled-section">
                <h3>Units ({payload.units.length})</h3>
                {#each payload.units as u}
                  <div class="recalled-item">
                    <span class="recalled-type-badge">{u.type ?? 'unit'}</span>
                    <span>{u.content ?? ''}</span>
                  </div>
                {/each}
              </div>
            {/if}
            {#if payload.entities && payload.entities.length}
              <div class="recalled-section">
                <h3>Entities ({payload.entities.length})</h3>
                {#each payload.entities as e}
                  <div class="recalled-item">
                    <div class="font-medium">{e.label ?? '(unnamed)'}</div>
                    {#if e.description}
                      <div class="text-xs text-gray-500 mt-0.5">{e.description}</div>
                    {/if}
                  </div>
                {/each}
              </div>
            {/if}
            {#if payload.relations && payload.relations.length}
              <div class="recalled-section">
                <h3>Relations ({payload.relations.length})</h3>
                {#each payload.relations as r}
                  <div class="recalled-item text-xs font-mono">
                    {r.subject ?? '?'} —
                    <span class="text-indigo-400">{r.predicate ?? '?'}</span>
                    — {r.object ?? '?'}
                  </div>
                {/each}
              </div>
            {/if}
            {#if (!payload.units || !payload.units.length) && (!payload.entities || !payload.entities.length) && (!payload.relations || !payload.relations.length) && !payload.query_text}
              <div class="text-gray-500 italic">No memories were recalled for this turn.</div>
            {/if}
            {#if externalUrl(payload)}
              <div
                class="recalled-section pt-3 border-t border-gray-200 dark:border-gray-800"
              >
                <a
                  class="text-xs text-indigo-400 hover:text-indigo-300"
                  href={externalUrl(payload)}
                  target="_blank"
                  rel="noopener">Open in Recordatio knowledge graph →</a
                >
              </div>
            {/if}
          {/if}
        {/if}
      </div>
    </div>
  </div>
{/if}
