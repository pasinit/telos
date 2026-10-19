/**
 * Auth state + token storage.
 *
 * Scaffolded in 7A so `lib/api.ts` can call `getBearerToken()` without
 * a forward reference. PR 7D (Login + Profile) fills in the real
 * login/logout helpers and the `currentUser` store consumed by
 * protected pages.
 *
 * Storage strategy:
 * - Browser: cookie set by the backend on login. The Bearer token is
 *   still stashed in `localStorage` so CLI-equivalent fetches and
 *   page-reload bootstrap work without a network roundtrip.
 * - CLI: callers set the token manually via `setBearerToken()`.
 *
 * The `currentUser` store is the single source of truth for "who is
 * logged in right now?" inside the SPA. It starts as `undefined` so
 * components can distinguish "not loaded yet" from "loaded, no user"
 * (null).
 */

import { get, writable, type Readable } from 'svelte/store';
import { apiGet, apiPost, ApiError } from './api';

const TOKEN_KEY = 'telos.bearer';

/**
 * Read the current bearer token, if any. Returns `null` when not
 * authenticated, when running outside a browser context, or when
 * localStorage is unavailable (Safari private mode).
 */
export function getBearerToken(): string | null {
  try {
    if (typeof localStorage === 'undefined') return null;
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setBearerToken(token: string | null): void {
  try {
    if (typeof localStorage === 'undefined') return;
    if (token === null) localStorage.removeItem(TOKEN_KEY);
    else localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // localStorage may throw in Safari private mode; treat as best-effort.
  }
}

// ──────────────────────────────────────────────────────────────────────
// Current-user store + auth API helpers (PR 7D)
// ──────────────────────────────────────────────────────────────────────

export interface User {
  uid: string;
}

interface LoginResponse {
  token: string;
  user: User;
}

/**
 * Reactive store of the logged-in user.
 *
 * Distinguishes three states:
 *   - `undefined` — boot, no /me call has resolved yet
 *   - `null`      — loaded, not authenticated
 *   - `User`      — loaded, authenticated
 */
const _user = writable<User | null | undefined>(undefined);

export const currentUser: Readable<User | null | undefined> = {
  subscribe: _user.subscribe,
};

/** Manually set the in-memory user (used after login). */
export function setCurrentUser(user: User | null): void {
  _user.set(user);
}

async function resetAgentsIfUserChanged(nextUser: User | null): Promise<void> {
  const previous = get(_user);
  if (previous !== undefined && previous?.uid === nextUser?.uid) return;

  // Agent rosters are scoped to the authenticated user. The store lives for
  // the whole SPA session, so identity changes must clear its cached list.
  const { resetAgents } = await import('./stores/agents');
  resetAgents();
}

/**
 * Probe `/api/auth/me` to discover whether we're authenticated.
 *
 * Returns the user on success, `null` on 401. Other errors propagate.
 * Side-effect: updates the `currentUser` store. Idempotent — safe to
 * call from multiple `onMount`s in the same boot.
 */
export async function loadMe(): Promise<User | null> {
  try {
    const me = await apiGet<User>('/auth/me');
    await resetAgentsIfUserChanged(me);
    _user.set(me);
    return me;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      await resetAgentsIfUserChanged(null);
      _user.set(null);
      return null;
    }
    throw e;
  }
}

/**
 * Submit credentials to `POST /api/auth/login`.
 *
 * On success: stashes the bearer token, updates `currentUser`, returns
 * the user. The server also sets an HttpOnly cookie for browser fetches;
 * the bearer is a belt-and-braces fallback for page-reload bootstrap and
 * for parity with the CLI surface.
 *
 * On failure: throws `ApiError` — callers render `e.message`.
 */
export async function login(username: string, email?: string): Promise<User> {
  const body: { username: string; email?: string } = { username };
  if (email && email.trim()) body.email = email.trim();
  const res = await apiPost<LoginResponse>('/auth/login', body);
  await resetAgentsIfUserChanged(res.user);
  setBearerToken(res.token);
  _user.set(res.user);
  return res.user;
}

/**
 * Clear the cookie server-side, drop the bearer locally, reset the store.
 *
 * Best-effort on the server side: even if the POST fails (network out,
 * 500), we still clear local state so the SPA's UI reflects "logged out".
 */
export async function logout(): Promise<void> {
  try {
    await apiPost('/auth/logout');
  } catch {
    // Swallow — local cleanup is the important part.
  }
  await resetAgentsIfUserChanged(null);
  setBearerToken(null);
  _user.set(null);
}
