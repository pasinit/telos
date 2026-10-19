# telos-web — conventions

The SPA half of Telos v2. This document is the source of truth for the
seven page-implementer PRs that follow PR 7A. Read it before opening a
PR. The relevant primers in `/tmp/phase7-expertise/` are the deep
dives; this doc is the "what does my code look like in this repo"
answer.

Sections:

1. [Goal & how to read this doc](#1-goal--how-to-read-this-doc)
2. [Stack](#2-stack)
3. [Directory layout](#3-directory-layout)
4. [Routing](#4-routing)
5. [Auth model](#5-auth-model)
6. [API client](#6-api-client)
7. [SSE / streaming](#7-sse--streaming)
8. [State (Svelte stores)](#8-state-svelte-stores)
9. [Styling](#9-styling)
10. [TypeScript discipline](#10-typescript-discipline)
11. [Svelte component style](#11-svelte-component-style)
12. [Markdown rendering](#12-markdown-rendering)
13. [PWA discipline](#13-pwa-discipline)
14. [Testing](#14-testing)
15. [Shared-file etiquette](#15-shared-file-etiquette)

---

## 1. Goal & how to read this doc

The goal of Phase 7 is to replace server-rendered Jinja with a Svelte
SPA that talks to the same FastAPI app over JSON, while keeping every
URL, every PWA-installed shortcut, and every bookmark working. This
file pins the cross-cutting decisions so seven implementers (one per
page) ship a coherent codebase instead of seven dialects.

**How to read this**: every page implementer should read all 15
sections end-to-end once before writing code. Each section is short on
purpose — the long primers in `/tmp/phase7-expertise/` cover the
rationale. Patterns that appear in code snippets here are mandatory;
deviations need approval from `reviewer-consistency`.

**Escape valve**: if you hit a case CONVENTIONS doesn't address, DM
`reviewer-consistency` BEFORE freelancing. Don't ship a new pattern
unilaterally — once it's in `main`, every later PR will copy it.

---

## 2. Stack

| Concern | Choice | Reason |
|---|---|---|
| UI framework | Svelte 5, **classic syntax** (no runes) | Smaller learning curve, all existing material applies. `export let`, `$:`, slots, stores. |
| Bundler | Vite 5 | Native ESM dev, fast HMR. |
| Language | TypeScript strict (`strict: true`, `noImplicitAny`, `noUnusedLocals`) | The backend has types; the SPA should too. |
| Router | `svelte-spa-router` ^4 | ~3 KB, history-mode, easy to test. Locked. |
| PWA | `vite-plugin-pwa` with `autoUpdate` | Day-one offline shell + auto-update. |
| Tests | Vitest + jsdom | One test runner, same syntax as vite. |
| HTTP | `fetch` via `lib/api.ts` wrapper | No axios. |
| State | Native Svelte stores | No Redux, no Zustand. |
| CSS | Tailwind via CDN tag + `app.css` | Mirrors what the SSR app already ships. |

`package.json` is canonical; don't add a dep without discussing in your
PR description. `svelte.config.js` sets `runes: false` — keep it that way.

---

## 3. Directory layout

```
telos-web/
├── CONVENTIONS.md            ← this file
├── index.html                ← Vite entry; owns iOS PWA meta tags
├── public/                   ← static assets copied verbatim to dist/
├── src/
│   ├── main.ts               ← mount App + registerSW
│   ├── App.svelte            ← <Router /> shell
│   ├── app.css               ← ported base.html styles (don't redesign)
│   ├── routes.ts             ← Map<path, component>
│   ├── lib/                  ← framework-free helpers
│   │   ├── api.ts            ← fetch wrapper (shared)
│   │   ├── auth.ts           ← token storage + user store (shared)
│   │   ├── sse.ts            ← chat streaming helper
│   │   ├── markdown.ts       ← marked wrapper
│   │   └── stores/           ← typed Svelte stores: <domain>.ts
│   ├── components/
│   │   ├── common/           ← used by ≥2 pages (Toast, Button, Modal…)
│   │   ├── Shell.svelte      ← sidebar + header (shared)
│   │   └── <PageName>/       ← used by exactly one page
│   └── pages/
│       └── <PageName>.svelte ← one .svelte per top-level route
└── dist/                     ← Vite output, gitignored
```

**Decision rules** for where a new file goes:

- It's a top-level route → `src/pages/<PageName>.svelte`. Register in `routes.ts`.
- It's reused inside that one page → `src/components/<PageName>/Foo.svelte`.
- It's reused by two or more pages → `src/components/common/Foo.svelte`. See §11 for the move-when-second-consumer rule.
- It's data fetching / parsing / pure logic → `src/lib/` (no DOM, no Svelte imports if avoidable). Helpers are unit-testable.
- It's domain state shared across components → `src/lib/stores/<domain>.ts`.

---

## 4. Routing

`svelte-spa-router` in history-mode. The router is mounted exactly once
in `App.svelte`; pages are entries in `routes.ts`.

```svelte
<!-- src/App.svelte -->
<script lang="ts">
  import Router from 'svelte-spa-router';
  import { routes } from './routes';
</script>
<Router {routes} restoreScrollState={true} />
```

```ts
// src/routes.ts — Map preserves insertion order; wildcard MUST be last.
import { wrap } from 'svelte-spa-router/wrap';
import Login from './pages/Login.svelte';
import NotFound from './pages/NotFound.svelte';

export const routes = new Map();
routes.set('/login', Login);
routes.set('/usage', wrap({ asyncComponent: () => import('./pages/Usage.svelte') }));
routes.set('/agents/:slug', wrap({ asyncComponent: () => import('./pages/ChatPage.svelte') }));
routes.set('*', NotFound);   // ← always last
```

**Hard rules:**

- Every `<a>` that points to an in-app route uses `use:link`. Forgetting
  it is an invisible bug that triggers a full reload and wipes stores.
  ```svelte
  <script>import { link } from 'svelte-spa-router';</script>
  <a href="/agents" use:link>Agents</a>
  ```
- Programmatic navigation: `push('/x')` adds history; `replace('/x')`
  swaps. Use `replace` for "you shouldn't be here" and for stripping
  query strings after consuming them (see §5 OAuth flow).
- Route params come from the `params` store: `$params.slug` (reactive).
  Always `$` it; reading `params.slug` returns the store object.
- Code-split heavy pages with `wrap({ asyncComponent: () => import(...) })`.
  Required for Chat (largest), Usage (markdown), and any page that
  pulls a chunky dep.

The history-mode catch-all that serves `index.html` for non-API paths
lives in the backend (PR 7B's `spa.py`). See the
`svelte-spa-router-patterns.md` primer for the gotchas list (you will
hit at least one of them — read it).

---

## 5. Auth model

Two equivalent credentials, single backend dependency (PR 7C):

- **Browser**: HttpOnly cookie set by `POST /api/auth/login`.
  Cookie attrs: `HttpOnly`, `SameSite=Lax`, `Secure` only when
  `PUBLIC_HOST` starts with `https://`, `Max-Age=31536000`.
- **CLI / scripts**: `Authorization: Bearer <token>`. Same token value
  the login endpoint returns in JSON.

Browser code attaches both — the bearer goes via `Authorization`
header, the cookie is sent same-origin. Backend accepts either.

```ts
// Pattern for protected pages — runs in onMount, no route-table guard.
import { onMount } from 'svelte';
import { replace } from 'svelte-spa-router';
import { apiGet, ApiError } from '../lib/api';

let data: unknown;
onMount(async () => {
  try {
    data = await apiGet('/agents');
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) { replace('/login'); return; }
    throw e;
  }
});
```

**Login flow** (PR 7D implements):

1. `POST /api/auth/login` with `{ username, email? }` → returns
   `{ token, user }` AND sets the cookie.
2. Stash the token via `setBearerToken(token)` in `lib/auth.ts`. The
   server cookie covers browser fetches; the localStorage copy lets
   page-reload / SW boot recover without a roundtrip.
3. `push('/')` to the home page.

**Logout**: `POST /api/auth/logout` (clears cookie server-side) →
`setBearerToken(null)` → `replace('/login')`.

**Google OAuth callback** (PR 7D implements on `Profile.svelte`):

The flow round-trips through Google as a full-page navigation, so the
SPA loses all in-memory state. The only surviving signal is the URL
query string. Documented here so PR 7D doesn't re-derive it.

```
Profile.svelte → user clicks "Connect Google"
   → window.location.href = '/api/oauth/google/start'   ← FULL-PAGE NAV, NOT use:link, NOT fetch
backend signs state, 303 → Google consent
   → user accepts, 303 → /api/oauth/google/callback?code&state
backend exchanges code, persists creds
   → 303 → /profile?google=connected   (or ?google=error)
SPA fresh-mounts Profile.svelte
   → reads $querystring, shows toast, replace('/profile') to strip the qs
```

The "Connect Google" button MUST be a real anchor (or
`window.location.href = ...`) — NOT a `<a use:link>` (that intercepts
and prevents the navigation Google needs). See
`/tmp/phase7-expertise/oauth-redirect-spa.md` for the full pattern
including state CSRF and the SameSite=Lax cookie requirement.

```svelte
<!-- canonical Profile.svelte mount handler — implement in PR 7D -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { querystring, replace } from 'svelte-spa-router';
  import { showToast } from '../lib/toast';
  onMount(() => {
    const qs = new URLSearchParams($querystring);
    const g = qs.get('google');
    if (g === 'connected') { showToast('Google connected ✓'); replace('/profile'); }
    else if (g === 'error') { showToast('Google connection failed', { kind: 'error' }); replace('/profile'); }
  });
</script>
<a href="/api/oauth/google/start">Connect Google</a>  <!-- no use:link -->
```

---

## 6. API client

Always go through `lib/api.ts`. **Never raw `fetch` for `/api/*`** —
the wrapper handles the bearer header, error shape, JSON content-type,
and `credentials: 'same-origin'` so the cookie always travels.

```ts
import { apiGet, apiPost, apiDelete, ApiError } from '../lib/api';

// GET
const agents = await apiGet<Agent[]>('/agents');

// POST with JSON body
const { slug } = await apiPost<{ slug: string }>('/agents', { name: 'cooking-coach' });

// DELETE
await apiDelete(`/agents/${slug}`);

// Error handling: ApiError carries .status and .body
try { await apiGet('/usage'); }
catch (e) {
  if (e instanceof ApiError && e.status === 401) replace('/login');
  else throw e;
}
```

**Rules:**

- Path argument is the part AFTER `/api`. The wrapper prepends `/api`
  for you. So `apiGet('/auth/me')` hits `/api/auth/me`. Paths that
  already start with `/api/` are accepted (no double prefix).
- Always include an explicit response type via the generic
  (`apiGet<Foo>('/path')`). Don't let TS infer `unknown` and then
  cast — the type IS the contract with the backend.
- For non-JSON endpoints (file upload, SSE), use the relevant helper
  (`FormData` body, `lib/sse.ts`) — don't bypass to raw fetch.

If you need a new helper (e.g. a typed alias for a frequently used
endpoint), DM `setup-impl` (owner of `lib/api.ts`) before adding.

---

## 7. SSE / streaming

The chat endpoint streams Server-Sent Events over `POST`. `EventSource`
is GET-only, so the helper in `lib/sse.ts` (PR 7E owns) uses `fetch` +
`ReadableStream` instead. **Every chat-like stream goes through this
helper** — do not roll your own.

```ts
// signature page implementers in PR 7E consume
import { streamSSE } from '../../lib/sse';

await streamSSE('/api/agents/cooking/chat', {
  body: { user_message: 'Hello' },
  onFrame: (frame) => {
    // frame is the parsed JSON from one `data: {...}` line
    console.log(frame.type, frame.data);
  },
  signal: abortController.signal,
});
```

**Rules:**

- Never cache an SSE response. The PWA SW config already excludes
  `/agents/*/chat` (NetworkOnly); don't override.
- Always pass an `AbortSignal` so the stream is torn down on
  unmount / navigation.
- Frame parsing lives in `lib/sse.ts`. Components consume the
  already-parsed objects via `onFrame`.

See `/tmp/phase7-expertise/sse-over-post-clientside.md` for the
full reader-loop pattern including iOS Safari quirks.

---

## 8. State (Svelte stores)

Use native Svelte stores. No Redux, no Zustand. Domain-scoped stores
live in `src/lib/stores/<domain>.ts`. Stores are module-scope (not
inside components) so they survive HMR.

```ts
// src/lib/stores/agents.ts — typed writable store
import { writable, type Writable, derived } from 'svelte/store';
import { apiGet } from '../api';

export interface Agent { slug: string; name: string }

export const agents: Writable<Agent[]> = writable([]);
export const agentBySlug = derived(agents, ($a) => new Map($a.map((x) => [x.slug, x])));

let loaded = false;
export async function ensureAgentsLoaded(): Promise<void> {
  if (loaded) return;
  agents.set(await apiGet<Agent[]>('/agents'));
  loaded = true;
}
```

**Rules:**

- Subscribe with `$store` syntax in components; manual `.subscribe()`
  only when you need an unsubscribe handle (rare).
- Cache-once stores expose an `ensure*Loaded()` helper instead of
  re-fetching on every component mount.
- When two pages both need a store you wrote inside one, MOVE it to
  `src/lib/stores/` — don't import across page folders.

---

## 9. Styling

Tailwind classes via the CDN tag in `index.html`. Custom rules
(prose, sidebar, PWA safe areas) live in `src/app.css`, ported
verbatim from `base.html`. **Do not redesign during the migration** —
visual parity with the SSR app is the bar.

```svelte
<button class="bg-brand hover:bg-brand-dark transition-colors rounded-lg py-2.5 text-sm font-semibold text-white">
  Continue
</button>
```

**Rules:**

- `app.css` is shared and rarely modified. DM `setup-impl` before
  touching it; new prose rules belong here, not scattered in
  components.
- Dark mode is toggled via the `dark` class on `<html>`. The
  pre-paint script in `index.html` sets it before paint to avoid a
  flash; don't duplicate that logic elsewhere.
- Mobile breakpoint is `768px` per `app.css` — match it if you add
  responsive styles.
- iOS PWA: `safe-top` / `safe-bottom` classes wrap the body in
  `index.html`. Use them where layout meets the notch.

---

## 10. TypeScript discipline

`tsconfig.json` is strict and adds `noUnusedLocals`,
`noUnusedParameters`, `noImplicitReturns`,
`noFallthroughCasesInSwitch`. The CI `npm run check` step blocks
merges on type errors.

```ts
// GOOD — explicit return + named interface
export interface User { uid: string; email: string | null }
export async function loadMe(): Promise<User> {
  return apiGet<User>('/auth/me');
}

// BAD — implicit any, casts away an unknown
const me = await fetch('/api/me').then((r) => r.json()) as any;
```

**Rules:**

- No `any` in committed code. If you must, prefix with `// eslint-disable`-style
  comment explaining the exact reason (e.g. third-party untyped lib).
- Parsed JSON is `unknown` until proven otherwise — narrow with
  type guards, not casts.
- Exported functions and store types are annotated explicitly. Inferred
  return types on private helpers are fine.
- Use `interface` for object shapes, `type` for unions/aliases.
- Path imports: relative within `src/`; no path aliases yet (revisit
  if `../../..` hits four levels — it shouldn't with the current layout).

---

## 11. Svelte component style

Svelte 5 with **classic syntax** (runes disabled in `svelte.config.js`).
Components use `<script lang="ts">`. Reactive blocks via `$:`. Props
via `export let`.

```svelte
<script lang="ts">
  import type { Agent } from '../lib/stores/agents';
  export let agent: Agent;
  export let onSelect: (slug: string) => void = () => {};

  $: label = agent.name.trim() || agent.slug;
</script>

<button class="text-left w-full" on:click={() => onSelect(agent.slug)}>
  {label}
</button>
```

**Rules:**

- Component file = PascalCase (`AgentRow.svelte`). One component per file.
- One default export per file (Svelte enforces this; just don't fight it).
- Prefer **slots** over render-prop callbacks for content composition.
  Use callback props (`onSelect`) for event-like leaf interactions.
- Don't reach into the DOM with `document.querySelector` from a
  component — use `bind:this` + an action.
- Async work goes in `onMount`; cleanup in the returned function.
- Keep components ≤200 lines. Split before they grow.

**Shared-component reuse rules:**

- Used by exactly one page → `components/<PageName>/Foo.svelte`,
  or co-located with the page if it's tiny (≤30 lines).
- Used by ≥2 pages → `components/common/Foo.svelte`.
- **When you're the SECOND page to need a component the first page
  already wrote in its local folder**: DM the first PR's owner. One
  of you moves it to `components/common/` in a small follow-up commit
  on whichever PR is still open. Don't fork-and-duplicate. Don't
  silently import across page folders.

---

## 12. Markdown rendering

One source of truth: `src/lib/markdown.ts` (PR 7E owns). It wraps
`marked` with the same sanitiser/feature set the SSR app uses today
(`md_to_html` in `telos/ui/markdown.py`).

```ts
// pattern — implemented in PR 7E
import { render } from '../lib/markdown';
const html = render(turn.agent_text);   // returns sanitized HTML string
```

```svelte
<div class="agent-response">{@html render(text)}</div>
```

**Rules:**

- Render via the wrapper, never `marked.parse(...)` directly in a
  component.
- `{@html ...}` is allowed ONLY for the markdown wrapper's output and
  ONLY inside an `.agent-response` container (so the prose CSS applies).
- For user-typed content (chat input echo, agent names), never
  `{@html}` — Svelte's default `{value}` escaping is the right call.

---

## 13. PWA discipline

`vite-plugin-pwa` with `autoUpdate`. Service worker is generated by
Workbox; we do **not** maintain a hand-written SW. Configuration lives
in `vite.config.ts`. PR 7L will revisit caching strategies.

**Rules:**

- `/api/*` → `NetworkOnly`. Already configured. **Don't override.**
  Caching user-scoped JSON across users is a data leak.
- Chat SSE (`/agents/*/chat`) → `NetworkOnly`. Same reason + streams
  can't be cached anyway.
- New static assets: drop in `telos-web/public/` and reference from
  `/` — Vite copies to `dist/`, Workbox precaches.
- iOS PWA meta tags live in `index.html` (`apple-mobile-web-app-*`,
  `apple-touch-icon`, viewport `viewport-fit=cover`). Don't move
  them to a Svelte component — they must be present at first paint
  and survive SW updates.
- `start_url` is `/` in the manifest. Changing this requires every
  installed user to reinstall — don't.

See `/tmp/phase7-expertise/vite-plugin-pwa-config.md` for cache
invalidation across the cutover (PR 7L territory).

---

## 14. Testing

- Vitest for `lib/` helpers, stores, and parsers. Every non-trivial
  pure function gets a test.
- Component tests are optional; manual browser smoke is acceptable
  for UI-only changes if the diff is small and the convention exists
  elsewhere.
- Tests live next to the unit: `lib/api.ts` ↔ `lib/api.test.ts`.
- `npm test` runs the full suite in CI. CI also runs `npm run check`
  (svelte-check) and `npm run build`.

```ts
// shape — one describe per module, one it per behaviour
import { describe, it, expect } from 'vitest';
import { foo } from './foo';

describe('foo', () => {
  it('returns x for y', () => {
    expect(foo('y')).toBe('x');
  });
});
```

Backend tests stay in pytest. The backend CI path is separate.

---

## 15. Shared-file etiquette

These files are touched by multiple PRs. **DM the owner before
modifying.** Owner approves in message. PR body's
`## Shared-file changes` section must list each edit with the
approver name.

| File | Owner (during 7A–7C) | Owner (after) |
|---|---|---|
| `src/App.svelte` | setup-impl | page-1-login (after 7D) |
| `src/routes.ts` | setup-impl | each PR appends one entry; coordinate via DM if line collisions |
| `src/lib/api.ts` | setup-impl | additions only; DM before |
| `src/lib/auth.ts` | setup-impl | page-1-login (after 7D) |
| `src/components/Shell.svelte` | setup-impl | page-1-login (after 7D) |
| `src/app.css` | setup-impl | modifications discouraged; DM before |
| `telos/ui/app.py` | setup-impl (7B–7C) | each page PR appends `include_router(...)` |

If a file is missing from this table and ≥2 PRs need it, DM
`reviewer-consistency` to add a row.

**Protocol** (also in `~/.claude/teams/phase7/file-ownership.md`):

1. Implementer drafts the change on their branch.
2. `SendMessage(to: <owner>, …)` with intent + the proposed diff.
3. Owner replies APPROVED or with revisions. ≤1 round expected.
4. Implementer commits.
5. PR body includes `## Shared-file changes` section listing each
   modification with the approver.
6. `reviewer-consistency` cross-checks PR descriptions against the
   actual shared-file diffs.

---

## Further reading

- `/tmp/phase7-expertise/svelte-spa-router-patterns.md` — routing deep dive
- `/tmp/phase7-expertise/vite-plugin-pwa-config.md` — PWA / caching / cutover
- `/tmp/phase7-expertise/sse-over-post-clientside.md` — chat stream client
- `/tmp/phase7-expertise/cookie-attrs-fastapi.md` — backend auth cookie attrs (PR 7C)
- `/tmp/phase7-expertise/oauth-redirect-spa.md` — Google OAuth callback (PR 7D)
- `/tmp/phase7-expertise/dockerfile-multistage-node-python.md` — single-container build (PR 7B)
- `~/.claude/teams/phase7/plan.md` — full Phase 7 spec
- `~/.claude/teams/phase7/file-ownership.md` — per-PR file ownership map
