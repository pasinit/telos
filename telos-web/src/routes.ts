/**
 * SPA route table.
 *
 * `Map` preserves insertion order; the wildcard `*` route MUST be last
 * or it will swallow every other path. Object literals don't guarantee
 * ordering — keep this as a Map even if we only had one route.
 *
 * Page-implementer PRs append entries here. Coordinate via DM if two
 * PRs target the same line.
 *
 * PR 7E entries:
 * - `/` → Housekeeper (replaces the 7A Login smoke target)
 * - `/agents/:slug` → ChatPage (lazy-loaded via `wrap`)
 */
import type { ComponentType, SvelteComponent } from 'svelte';
import { wrap } from 'svelte-spa-router/wrap';
import Housekeeper from './pages/Housekeeper.svelte';
import Login from './pages/Login.svelte';
import Profile from './pages/Profile.svelte';
import NotFound from './pages/NotFound.svelte';

// Type alias matching svelte-spa-router's route value.
export type Route = ComponentType<SvelteComponent> | { component?: unknown; asyncComponent?: () => Promise<unknown> };

export const routes = new Map<string | RegExp, Route>();

// Auth / shell pages
routes.set('/login', Login as unknown as Route);
routes.set('/profile', Profile as unknown as Route);

// Housekeeper is the post-login home — the SPA's primary landing.
routes.set('/', Housekeeper as unknown as Route);

// Agents index — PR 7F. Code-split so the housekeeper landing page
// and Login don't pull the create-form code on first paint.
routes.set('/agents', wrap({
  asyncComponent: () => import('./pages/AgentsIndex.svelte'),
}) as unknown as Route);

// Improvements (PR 7H). Code-split — the per-run details + diff blocks
// pull a chunk of markup that idle agents don't need. Registered
// before `/agents/:slug` so the more-specific path takes precedence
// in case the router walks the map in insertion order.
routes.set(
  '/agents/:slug/improvements',
  wrap({ asyncComponent: () => import('./pages/Improvements.svelte') }) as unknown as Route,
);

// Settings page — registered BEFORE `/agents/:slug` so the
// more-specific `/agents/:slug/settings` match wins if the router
// walks the Map in insertion order (same rationale as 7H's
// `/improvements` ordering note above).
routes.set(
  '/agents/:slug/settings',
  wrap({ asyncComponent: () => import('./pages/AgentSettings.svelte') }) as unknown as Route,
);

// Nightly feedback — per-agent overnight-analysis history. Code-split
// like the sibling agent sub-pages; registered before `/agents/:slug`
// so the more-specific path wins on insertion-order walks.
routes.set(
  '/agents/:slug/nightly',
  wrap({ asyncComponent: () => import('./pages/AgentNightly.svelte') }) as unknown as Route,
);

// Specialist chat — code-split because Chat.svelte is the heaviest
// page (SSE + markdown + recordatio drawer + tool display).
routes.set('/agents/:slug', wrap({ asyncComponent: () => import('./pages/ChatPage.svelte') }) as unknown as Route);

// Usage page (PR 7I). Code-split because `lib/usage.ts` + the
// `components/Usage/*` tree are chunky enough to be worth lazy-loading
// per CONVENTIONS §4 ("Required for Chat (largest), Usage (markdown),
// and any page that pulls a chunky dep").
routes.set('/usage', wrap({ asyncComponent: () => import('./pages/Usage.svelte') }) as unknown as Route);

// Wildcard fallback — MUST stay last.
routes.set('*', NotFound as unknown as Route);
