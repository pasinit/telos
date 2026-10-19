<script lang="ts">
  /**
   * App shell — sidebar + drawer + main slot.
   *
   * Owned by page-1-login from PR 7D onward. Ports the chat-page
   * sidebar structure from `templates/chat.html` so any page that
   * wraps in `<Shell>` gets the same nav out of the box:
   *
   *   - Brand row (logo + τέλος wordmark)
   *   - Housekeeper link (`/`)
   *   - Specialist agents list (from `lib/stores/agents.ts`)
   *   - Manage agents + Usage links
   *   - Theme cycle button
   *   - Profile link with current uid
   *   - Sign out
   *
   * Page-specific affordances (chat's "Settings" / "Clear history",
   * the agent name in the header) are slotted by the page via the
   * `header` and `footer` slots. The default slot is the page body.
   *
   * Mobile drawer: at <=768px the sidebar collapses behind a `☰`
   * button in the header. CSS lives in `app.css` (`.sidebar`,
   * `.sidebar-overlay`, `.mobile-header`) — ported from `base.html`
   * in PR 7A, so this component only toggles the `.open` class.
   *
   * The shell is NOT mounted globally — Login / Profile (full-screen
   * forms) render without it, mirroring the SSR pages. Later page
   * PRs (7E chat, 7F agents index, 7G settings, 7H improvements,
   * 7I usage) wrap their content in `<Shell>...</Shell>`.
   */
  import { onMount } from 'svelte';
  import { link, push } from 'svelte-spa-router';
  import { agents, ensureAgentsLoaded, resetAgents } from '../lib/stores/agents';
  import { currentUser, logout } from '../lib/auth';
  import { showToast } from '../lib/toast';
  import { cycleTheme, themeIcon, themeLabel, themeMode } from '../lib/theme';

  /**
   * Slug of the agent currently being viewed (for sidebar
   * highlighting). Pages that aren't agent-scoped (settings, usage,
   * etc.) leave it undefined.
   */
  export let activeAgentSlug: string | undefined = undefined;
  /** True when the page is the Housekeeper chat (highlights the row). */
  export let isHousekeeper: boolean = false;

  let drawerOpen = false;

  onMount(() => {
    // Best-effort agent fetch. The store swallows 404s on its own —
    // we still wrap in try/catch so a transient network blip doesn't
    // crash the page.
    void ensureAgentsLoaded().catch(() => {});
  });

  function toggleDrawer(): void {
    drawerOpen = !drawerOpen;
  }
  function closeDrawer(): void {
    drawerOpen = false;
  }

  function onAgentClick(): void {
    // Mobile: tapping a nav item dismisses the drawer so the page
    // content is visible. Desktop CSS ignores the `.open` class.
    closeDrawer();
  }

  async function onSignOut(): Promise<void> {
    await logout();
    resetAgents();
    showToast('Signed out.', { kind: 'info' });
    push('/login');
  }

  function onCycleTheme(): void {
    cycleTheme();
  }
</script>

<!-- Mobile drawer overlay (CSS hides on desktop) -->
<button
  type="button"
  class="sidebar-overlay {drawerOpen ? 'open' : ''}"
  aria-label="Close menu"
  on:click={closeDrawer}
></button>

<!-- Sidebar -->
<aside
  class="sidebar w-64 shrink-0 flex flex-col border-r border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 h-full {drawerOpen ? 'open' : ''}"
>
  <div class="px-5 py-5 border-b border-gray-200 dark:border-gray-800">
    <a href="/" use:link on:click={closeDrawer} class="flex items-center gap-2 hover:opacity-80 transition-opacity">
      <img src="/icon-dark.svg" alt="Telos" class="w-7 h-7 rounded-md" />
      <span
        class="text-xl font-semibold"
        style="font-family:'Cormorant Garamond',serif;color:#b8941f;"
      >τέλος</span>
    </a>
  </div>

  <nav class="flex-1 overflow-y-auto px-3 py-4 space-y-1">
    <!-- Housekeeper link -->
    <a
      href="/"
      use:link
      on:click={onAgentClick}
      class="flex items-center gap-2 px-3 py-2 rounded-lg transition-colors group mb-2 {isHousekeeper
        ? 'bg-amber-100 dark:bg-amber-900/30 border border-amber-300 dark:border-amber-700/50'
        : 'hover:bg-amber-50 dark:hover:bg-amber-900/20'}"
    >
      <span style="color:#b8941f;">⌂</span>
      <span class="text-sm font-semibold truncate" style="color:#b8941f;">Housekeeper</span>
    </a>

    <div class="border-b border-gray-200 dark:border-gray-800 mb-2"></div>

    <!-- Specialist agents (graceful empty list when 7F not merged yet) -->
    {#each $agents.filter((a) => a.slug !== '_housekeeper') as agent (agent.slug)}
      <a
        href={`/agents/${agent.slug}`}
        use:link
        on:click={onAgentClick}
        class="flex items-center justify-between px-3 py-2 rounded-lg transition-colors group {!isHousekeeper && activeAgentSlug === agent.slug
          ? 'bg-gray-200 dark:bg-gray-800'
          : 'hover:bg-gray-200 dark:hover:bg-gray-800'}"
      >
        <span class="text-sm font-medium text-gray-700 dark:text-gray-200 truncate">{agent.name}</span>
      </a>
    {/each}

    <a
      href="/agents"
      use:link
      on:click={onAgentClick}
      class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 mt-2"
    >
      <span>+</span> Manage agents
    </a>
    <a
      href="/usage"
      use:link
      on:click={onAgentClick}
      class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
    >
      <span>$</span> Usage
    </a>
  </nav>

  <div class="px-4 py-3 border-t border-gray-200 dark:border-gray-800 space-y-1">
    <!-- Page-specific footer affordances (e.g. chat's Settings / Clear history) -->
    <slot name="sidebarFooter" />

    <button
      type="button"
      on:click={onCycleTheme}
      class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 w-full text-left"
      aria-label="Cycle theme"
    >
      <span class="theme-icon">{themeIcon($themeMode)}</span>
      <span class="theme-label">{themeLabel($themeMode)}</span>
    </button>

    <a
      href="/profile"
      use:link
      on:click={closeDrawer}
      class="flex items-center gap-2 px-3 py-1.5 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
    >
      <span class="text-gray-700 dark:text-gray-300 font-medium truncate">{$currentUser?.uid ?? ''}</span>
      <span class="opacity-70">·</span>
      <span>Profile</span>
    </a>

    <button
      type="button"
      on:click={onSignOut}
      class="flex items-center gap-2 px-3 py-1.5 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 w-full text-left"
    >
      Sign out
    </button>
  </div>
</aside>

<!-- Main content area -->
<div class="flex-1 flex flex-col h-full min-w-0">
  <!-- Mobile-only header bar with drawer toggle. CSS hides on desktop. -->
  <header class="mobile-header hidden items-center gap-3 px-4 py-3 border-b border-gray-200 dark:border-gray-800 shrink-0">
    <button
      type="button"
      on:click={toggleDrawer}
      class="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400"
      aria-label="Open menu"
      aria-expanded={drawerOpen}
    >
      ☰
    </button>
    <!-- Page-supplied header content (agent name, breadcrumb, etc.) -->
    <slot name="mobileHeader" />
  </header>

  <slot />
</div>
