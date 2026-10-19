<script lang="ts">
  /**
   * Per-agent improvement-run history.
   *
   * Mirrors the SSR ``improvements.html`` template but keeps the data
   * fresh via the polling store in ``lib/stores/improvements``. Runs in
   * the background appear without needing a full reload.
   *
   * Route param: ``slug`` (specialist agent slug, or ``_housekeeper``).
   *
   * Layout: wraps in the shared ``<Shell>`` (sidebar + mobile drawer +
   * nav + theme/profile/signout). Page-specific content goes in the
   * default slot; the agent name lands in the ``mobileHeader`` slot so
   * the mobile breadcrumb shows context.
   *
   * Agent meta (display name, description) is not part of the
   * improvements payload — PR 7E owns the future ``GET
   * /api/agents/{slug}`` meta endpoint. Until that lands the header
   * falls back to the slug; the housekeeper check is a constant
   * comparison since the slug is the same on both sides.
   */
  import { onDestroy } from 'svelte';
  import { params, replace } from 'svelte-spa-router';

  import { ApiError } from '../lib/api';
  import Shell from '../components/Shell.svelte';
  import DecisionFilter from '../components/Improvements/DecisionFilter.svelte';
  import RunsList from '../components/Improvements/RunsList.svelte';
  import {
    createImprovementsStore,
    type Decision,
    type ImprovementsState,
    type ImprovementsStore,
  } from '../lib/stores/improvements';

  /** Matches ``telos.housekeeper.HOUSEKEEPER_SLUG``. */
  const HOUSEKEEPER_SLUG = '_housekeeper';

  let store: ImprovementsStore | null = null;
  let currentSlug: string | null = null;

  // Reactive subscription target: `store && $store` only re-evaluates
  // when the store handle changes; the Svelte `$` prefix gives a live
  // view onto the state writable inside the store. The explicit
  // ImprovementsState | null annotation keeps svelte-check from
  // narrowing the type to `never` when `store` is initially null.
  $: storeState = (store ? $store : null) as ImprovementsState | null;
  $: slugParam = ($params?.slug as string | undefined) ?? null;
  $: isHousekeeper = slugParam === HOUSEKEEPER_SLUG;
  $: headerLabel = slugParam ?? '';

  // (Re)create the store whenever the slug changes. svelte-spa-router
  // reuses the page component when navigating between same-route URLs
  // with different params, so the slug→store binding must be reactive.
  // The reactive `$:` block fires on initial mount too, so there is no
  // separate `onMount` — that would double-build the store and
  // immediately abort the first one's in-flight fetch.
  $: if (slugParam && slugParam !== currentSlug) {
    rebuildStore(slugParam);
  }

  function rebuildStore(slug: string): void {
    store?.stop();
    currentSlug = slug;
    store = createImprovementsStore(slug);
    void store.refresh();
    store.start();
  }

  onDestroy(() => {
    store?.stop();
  });

  function handleDecision(next: Decision | null): void {
    store?.setDecision(next);
  }

  // 401 → kick to login. The store surfaces ApiError into `error`.
  $: if (storeState?.error instanceof ApiError && storeState.error.status === 401) {
    replace('/login');
  }
</script>

<Shell activeAgentSlug={slugParam ?? undefined} {isHousekeeper}>
  <svelte:fragment slot="mobileHeader">
    <span class="text-sm text-gray-600 dark:text-gray-400 truncate">
      {headerLabel} — improvements
    </span>
  </svelte:fragment>

  <main class="flex-1 overflow-y-auto">
    <div class="max-w-4xl mx-auto px-4 md:px-8 py-6 md:py-10 space-y-6">
      <h1 class="text-xl font-bold">{headerLabel} — improvement runs</h1>

      <DecisionFilter
        value={store?.getDecision() ?? null}
        runCount={storeState?.data?.runs.length ?? 0}
        onChange={handleDecision}
      />

      {#if storeState?.error && !(storeState.error instanceof ApiError && storeState.error.status === 401)}
        <div class="bg-rose-50 dark:bg-rose-950/40 border border-rose-200 dark:border-rose-900/50 rounded-lg p-3 text-sm text-rose-700 dark:text-rose-300">
          Couldn't load improvements: {storeState.error.message}
        </div>
      {/if}

      {#if storeState?.data}
        <RunsList runs={storeState.data.runs} />
      {:else if storeState?.loading}
        <div class="text-sm text-gray-500 dark:text-gray-400">Loading improvement runs…</div>
      {/if}
    </div>
  </main>
</Shell>
