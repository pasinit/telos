<script lang="ts">
  /**
   * Per-agent nightly-feedback history.
   *
   * Report-only: shows the overnight analyst's verdicts for this agent
   * (score, issues, classified recall misses, prompt suggestions). Route
   * param ``slug``. Structure mirrors ``Improvements.svelte`` — reactive
   * store rebuild on slug change, shared ``<Shell>`` chrome — minus the
   * poller (the nightly loop fires once a day, so one fetch on mount).
   */
  import { onDestroy } from 'svelte';
  import { params, replace } from 'svelte-spa-router';

  import { ApiError } from '../lib/api';
  import Shell from '../components/Shell.svelte';
  import NightlyList from '../components/Nightly/NightlyList.svelte';
  import { createNightlyStore, type NightlyState, type NightlyStore } from '../lib/stores/nightly';

  const HOUSEKEEPER_SLUG = '_housekeeper';

  let store: NightlyStore | null = null;
  let currentSlug: string | null = null;

  $: storeState = (store ? $store : null) as NightlyState | null;
  $: slugParam = ($params?.slug as string | undefined) ?? null;
  $: isHousekeeper = slugParam === HOUSEKEEPER_SLUG;
  $: headerLabel = slugParam ?? '';

  $: if (slugParam && slugParam !== currentSlug) {
    rebuildStore(slugParam);
  }

  function rebuildStore(slug: string): void {
    store?.stop();
    currentSlug = slug;
    store = createNightlyStore(slug);
    void store.refresh();
  }

  onDestroy(() => {
    store?.stop();
  });

  $: if (storeState?.error instanceof ApiError && storeState.error.status === 401) {
    replace('/login');
  }
</script>

<Shell activeAgentSlug={slugParam ?? undefined} {isHousekeeper}>
  <svelte:fragment slot="mobileHeader">
    <span class="text-sm text-gray-600 dark:text-gray-400 truncate">
      {headerLabel} — nightly feedback
    </span>
  </svelte:fragment>

  <main class="flex-1 overflow-y-auto">
    <div class="max-w-4xl mx-auto px-4 md:px-8 py-6 md:py-10 space-y-6">
      <div>
        <h1 class="text-xl font-bold">{headerLabel} — nightly feedback</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
          Overnight review of this agent's conversations. Report-only — nothing here changes the agent.
        </p>
      </div>

      {#if storeState?.error && !(storeState.error instanceof ApiError && storeState.error.status === 401)}
        <div class="bg-rose-50 dark:bg-rose-950/40 border border-rose-200 dark:border-rose-900/50 rounded-lg p-3 text-sm text-rose-700 dark:text-rose-300">
          Couldn't load nightly feedback: {storeState.error.message}
        </div>
      {/if}

      {#if storeState?.data}
        <NightlyList rows={storeState.data.feedback} />
      {:else if storeState?.loading}
        <div class="text-sm text-gray-500 dark:text-gray-400">Loading nightly feedback…</div>
      {/if}
    </div>
  </main>
</Shell>
