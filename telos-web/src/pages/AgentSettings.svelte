<script lang="ts">
  /**
   * Agent settings page — ``/agents/:slug/settings``.
   *
   * Reads the JSON payload from ``GET /api/agents/{slug}/settings``
   * (PR 7G) and composes a small set of presentational sub-components
   * from ``components/Settings/``. Mutations (approve / reject /
   * rollback / tools / delete) call the legacy SSR endpoints with
   * ``Accept: application/json`` — the backend now returns
   * ``{ok, new_prompt}`` for that path while preserving the 303
   * redirect for legacy form-POSTs (see plan §7G).
   *
   * Routines panel is a placeholder until PR 7J swaps in the full
   * management UI.
   */
  import { link, params, push, replace } from 'svelte-spa-router';
  import { api, apiDelete, apiGet, apiPost, ApiError } from '../lib/api';

  import Shell from '../components/Shell.svelte';
  import CurrentPromptPanel from '../components/Settings/CurrentPromptPanel.svelte';
  import DangerZone from '../components/Settings/DangerZone.svelte';
  import PendingCandidatePanel from '../components/Settings/PendingCandidatePanel.svelte';
  import PromptHistoryPanel from '../components/Settings/PromptHistoryPanel.svelte';
  import RoutinesPanel from '../components/Routines/RoutinesPanel.svelte';
  import SignalSummaryPanel from '../components/Settings/SignalSummaryPanel.svelte';
  import ToolsPicker from '../components/Settings/ToolsPicker.svelte';
  import type { SettingsPayload } from '../components/Settings/types';

  let payload: SettingsPayload | null = null;
  let loading = true;
  let error: string | null = null;
  let submitting = false;

  $: slug = ($params as { slug?: string } | undefined)?.slug ?? '';

  async function load(): Promise<void> {
    if (!slug) return;
    loading = true;
    error = null;
    try {
      payload = await apiGet<SettingsPayload>(`/agents/${slug}/settings`);
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.status === 401) { replace('/login'); return; }
        if (e.status === 404) { error = 'Agent not found.'; return; }
        error = `Failed to load settings (HTTP ${e.status}).`;
      } else {
        error = 'Failed to load settings.';
      }
    } finally {
      loading = false;
    }
  }

  // Reactive load: fires on initial mount (once `$: slug` is evaluated)
  // AND re-fires whenever the slug param changes (svelte-spa-router
  // reuses the component instance on same-route navigation). Using `$:`
  // alone — without a separate `onMount(load)` — keeps us from firing
  // ``load`` twice on first render.
  $: if (slug) { void load(); }

  // Mutations -----------------------------------------------------------

  async function approve(): Promise<void> {
    submitting = true;
    try {
      await apiPost(`/agents/${slug}/approve`);
      await load();
    } catch (e) {
      reportError(e);
    } finally {
      submitting = false;
    }
  }

  async function reject(): Promise<void> {
    submitting = true;
    try {
      await apiPost(`/agents/${slug}/reject`);
      await load();
    } catch (e) {
      reportError(e);
    } finally {
      submitting = false;
    }
  }

  async function rollback(sha: string): Promise<void> {
    submitting = true;
    try {
      // The legacy endpoint uses a FormData ``sha`` field, so the SPA
      // submits the same shape — ``api`` accepts FormData bodies.
      const fd = new FormData();
      fd.set('sha', sha);
      await api(`/agents/${slug}/rollback`, { method: 'POST', body: fd });
      await load();
    } catch (e) {
      reportError(e);
    } finally {
      submitting = false;
    }
  }

  async function saveTools(tools: string[]): Promise<void> {
    await apiPost(`/agents/${slug}/tools`, { tools });
    // Re-pull so any side effects (e.g. agent cache eviction) are
    // reflected, and so the baseline in <ToolsPicker> resets.
    await load();
  }

  async function deleteAgent(): Promise<void> {
    if (!window.confirm('Delete this agent? This cannot be undone.')) return;
    submitting = true;
    try {
      await apiDelete(`/agents/${slug}`);
      push('/');
    } catch (e) {
      reportError(e);
      submitting = false;
    }
  }

  function reportError(e: unknown): void {
    if (e instanceof ApiError) error = `Request failed (HTTP ${e.status}).`;
    else error = 'Request failed.';
  }
</script>

<!--
  Wrap in <Shell> per the 7F precedent. The settings page is
  agent-scoped, so `activeAgentSlug` tells the sidebar to highlight
  this agent. `sidebarFooter` carries the two "Back to chat" /
  "Improvement runs" quick-links that the legacy SSR template put in
  the bottom block of the sidebar — placement matches the SSR layout.
  `mobileHeader` gets the page title (the inline `<h1>` is still
  rendered for desktop; Shell's `.mobile-header` CSS hides on desktop).
-->
<Shell activeAgentSlug={slug}>
  <svelte:fragment slot="sidebarFooter">
    {#if payload}
      <a
        href={`/agents/${payload.agent.slug}`}
        use:link
        class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
      >
        <span>←</span> Back to chat
      </a>
      <a
        href={`/agents/${payload.agent.slug}/improvements`}
        use:link
        class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
      >
        <span>✦</span> Improvement runs
      </a>
      <a
        href={`/agents/${payload.agent.slug}/nightly`}
        use:link
        class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
      >
        <span>🌙</span> Nightly feedback
      </a>
    {/if}
  </svelte:fragment>
  <svelte:fragment slot="mobileHeader">
    {#if payload}
      <h1 class="text-sm font-semibold truncate">{payload.agent.name} — Settings</h1>
    {/if}
  </svelte:fragment>

  <div class="flex-1 overflow-y-auto">
    <div class="max-w-3xl mx-auto px-4 md:px-8 py-6 md:py-10 space-y-8">

      {#if loading && !payload}
        <p class="text-sm text-gray-500">Loading…</p>
      {:else if error && !payload}
        <div class="space-y-3">
          <p class="text-sm text-red-500">{error}</p>
          <a href="/" use:link class="text-sm text-indigo-500 hover:text-indigo-400 underline">Back to start</a>
        </div>
      {:else if payload}
        <div class="flex items-center gap-3">
          <h1 class="text-xl font-bold">{payload.agent.name} — settings</h1>
        </div>

        {#if error}
          <div class="text-xs text-red-500">{error}</div>
        {/if}

        {#if payload.has_pending && payload.candidate}
          <PendingCandidatePanel
            candidate={payload.candidate}
            {submitting}
            onApprove={approve}
            onReject={reject}
          />
        {/if}

        <CurrentPromptPanel prompt={payload.current_prompt} />

        <SignalSummaryPanel summary={payload.signal_summary} />

        <PromptHistoryPanel
          entries={payload.prompt_history}
          slug={payload.agent.slug}
          {submitting}
          onRollback={rollback}
        />

        <ToolsPicker
          groups={payload.tool_groups}
          initialSelected={payload.agent_tools}
          onSave={saveTools}
        />

        <RoutinesPanel slug={payload.agent.slug} routines={payload.routines} />

        <DangerZone {submitting} onDelete={deleteAgent} />
      {/if}

    </div>
  </div>
</Shell>
