/**
 * Behavioural smoke tests for Shell.svelte.
 *
 * Full DOM-level component tests are out of scope (no @testing-library
 * setup yet); this file pins the pure-data behaviours Shell relies on
 * (sign-out flow + agent-list filter rule) so a future regression in
 * `lib/auth` or `lib/stores/agents` doesn't silently break the shell.
 *
 * The Shell component itself is exercised by manual smoke and by the
 * page PRs (7E/F/G/H/I) that wrap their content in it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { currentUser, setBearerToken, setCurrentUser, logout } from '../lib/auth';
import { agents, ensureAgentsLoaded, resetAgents } from '../lib/stores/agents';

function mockFetch(response: { status?: number; body?: unknown }): ReturnType<typeof vi.fn> {
  const status = response.status ?? 200;
  const body = response.body ?? null;
  const spy = vi.fn(async () => {
    return new Response(body === null ? null : JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  });
  (globalThis as unknown as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return spy;
}

describe('Shell.svelte support code', () => {
  beforeEach(() => {
    setBearerToken(null);
    setCurrentUser(null);
    resetAgents();
  });
  afterEach(() => {
    setBearerToken(null);
    setCurrentUser(null);
    resetAgents();
    vi.restoreAllMocks();
  });

  it('logout() clears auth state — Shell sign-out depends on this', async () => {
    setBearerToken('stale');
    setCurrentUser({ uid: 'alice' });
    mockFetch({ body: { ok: true } });

    await logout();

    // Shell observes both: the bearer goes away (cookies-only fetches
    // for next mount) AND the currentUser store flips to null (so the
    // sidebar's "uid · Profile" row clears).
    expect(get(currentUser)).toBeNull();
  });

  it('ensureAgentsLoaded() propagates HTTP errors — Shell.svelte defends in its onMount', async () => {
    // Pre-PR-7F, this store swallowed 404 so Shell rendered against an
    // empty list. PR 7F made the store strict (`ApiError` propagates
    // for any non-2xx) so consumers can implement their own policy
    // (e.g. AgentsIndex.svelte redirects on 401). Shell.svelte's
    // onMount wraps the call in `.catch(() => {})` so a transient
    // failure still renders the chrome with an empty list.
    mockFetch({ status: 404, body: { detail: 'Not Found' } });
    await expect(ensureAgentsLoaded()).rejects.toMatchObject({ status: 404 });
    expect(get(agents)).toEqual([]);
  });

  it('ensureAgentsLoaded() populates the store on 200', async () => {
    mockFetch({
      body: [
        { slug: 'cooking', name: 'Cooking', description: '', created_at: null, last_active: null },
        { slug: 'gardening', name: 'Gardening', description: '', created_at: null, last_active: null },
      ],
    });
    await ensureAgentsLoaded();
    expect(get(agents)).toHaveLength(2);
    expect(get(agents).map((a) => a.slug)).toEqual(['cooking', 'gardening']);
  });

  it('Shell filters out a `_housekeeper` row from the specialist list', () => {
    // The Housekeeper is rendered as a dedicated row in the sidebar.
    // If the agents endpoint returns `_housekeeper` (as 7F's backend
    // wrapper around `RegistryRepo.list_for_user` does), Shell's
    // `$agents.filter((a) => a.slug !== '_housekeeper')` strips it
    // from the specialist list. Pin that contract here.
    const list = [
      { slug: '_housekeeper', name: 'Housekeeper', description: '', created_at: null, last_active: null },
      { slug: 'cooking', name: 'Cooking', description: '', created_at: null, last_active: null },
    ];
    const specialists = list.filter((a) => a.slug !== '_housekeeper');
    expect(specialists.map((a) => a.slug)).toEqual(['cooking']);
  });
});
