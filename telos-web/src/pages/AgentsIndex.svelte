<script lang="ts">
  /**
   * Agents index — `/agents`.
   *
   * Renders the user's specialist agents in a 1- or 2-column grid and
   * provides the "new agent" form. The housekeeper row (slug
   * `_housekeeper`) is filtered out client-side — it lives at `/`,
   * not in the index.
   *
   * Visual parity with the SSR template at
   * `telos/ui/templates/index.html`. Don't redesign during the
   * migration (CONVENTIONS §9).
   */
  import { onMount } from 'svelte';
  import { link, push, replace } from 'svelte-spa-router';

  import Shell from '../components/Shell.svelte';
  import { ApiError } from '../lib/api';
  import {
    type Agent,
    agents,
    createAgent,
    ensureAgentsLoaded,
  } from '../lib/stores/agents';

  const HOUSEKEEPER_SLUG = '_housekeeper';

  let name = '';
  let description = '';
  let submitting = false;
  let listError: string | null = null;
  let formError: string | null = null;

  $: specialists = ($agents as Agent[]).filter((a) => a.slug !== HOUSEKEEPER_SLUG);

  onMount(async () => {
    try {
      await ensureAgentsLoaded();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        replace('/login');
        return;
      }
      listError = e instanceof Error ? e.message : 'Failed to load agents';
    }
  });

  async function handleSubmit() {
    if (submitting) return;
    const trimmedName = name.trim();
    const trimmedDescription = description.trim();
    if (!trimmedName || !trimmedDescription) {
      formError = 'Name and goal are required';
      return;
    }
    submitting = true;
    formError = null;
    try {
      const created = await createAgent(trimmedName, trimmedDescription);
      push(`/agents/${created.slug}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        replace('/login');
        return;
      }
      formError = e instanceof Error ? e.message : 'Could not create agent';
    } finally {
      submitting = false;
    }
  }

  function preview(text: string | undefined, n = 100): string {
    const s = (text ?? '').trim();
    return s.length > n ? `${s.slice(0, n)}…` : s;
  }
</script>

<Shell>
  <main class="flex-1 overflow-y-auto">
    <div class="max-w-3xl mx-auto px-4 md:px-8 py-6 md:py-10">
      <div class="flex items-center justify-between mb-8">
        <div class="flex items-center gap-3">
          <h1 class="text-2xl font-bold">All agents</h1>
        </div>
      </div>

      {#if listError}
        <div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-300 rounded-lg p-4 mb-6 text-sm">
          {listError}
        </div>
      {/if}

      <!-- Create form -->
      <div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-gray-200 dark:border-gray-800 mb-10">
        <h3 class="font-semibold mb-5">New agent</h3>
        <form on:submit|preventDefault={handleSubmit} class="space-y-4">
          <div>
            <label class="block text-sm text-gray-500 dark:text-gray-400 mb-1.5" for="agent-name">Name</label>
            <input
              id="agent-name"
              type="text"
              bind:value={name}
              required
              disabled={submitting}
              placeholder="e.g. Head Hunter, Travel Assistant, Financial Advisor"
              class="w-full bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-transparent placeholder-gray-400 dark:placeholder-gray-600"
            />
          </div>
          <div>
            <label class="block text-sm text-gray-500 dark:text-gray-400 mb-1.5" for="agent-goal">Goal</label>
            <textarea
              id="agent-goal"
              bind:value={description}
              required
              disabled={submitting}
              rows="4"
              placeholder="Describe what this agent should help you with..."
              class="w-full bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-transparent placeholder-gray-400 dark:placeholder-gray-600 resize-y"
            ></textarea>
          </div>
          {#if formError}
            <p class="text-sm text-red-600 dark:text-red-400">{formError}</p>
          {/if}
          <button
            type="submit"
            disabled={submitting}
            class="bg-brand hover:bg-brand-dark transition-colors rounded-lg px-5 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
          >
            {submitting ? 'Creating…' : 'Create agent'}
          </button>
        </form>
      </div>

      <!-- Agent grid -->
      {#if specialists.length > 0}
        <h3 class="font-semibold mb-4 text-gray-600 dark:text-gray-300">Your agents</h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          {#each specialists as agent (agent.slug)}
            <a
              href={`/agents/${agent.slug}`}
              use:link
              class="block bg-gray-100 dark:bg-gray-900 hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors rounded-xl p-5 border border-gray-200 dark:border-gray-800 hover:border-gray-300 dark:hover:border-gray-700"
            >
              <h2 class="font-semibold text-gray-800 dark:text-gray-100 mb-1">{agent.name}</h2>
              <p class="text-gray-500 dark:text-gray-400 text-sm line-clamp-2">{preview(agent.description)}</p>
              <p class="text-xs text-gray-400 dark:text-gray-600 mt-3">
                {agent.last_active ? agent.last_active.slice(0, 10) : ''}
              </p>
            </a>
          {/each}
        </div>
      {/if}
    </div>
  </main>
</Shell>
