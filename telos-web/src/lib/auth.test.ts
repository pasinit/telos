/**
 * Tests for `lib/auth.ts` — the login/logout/loadMe helpers added in
 * PR 7D. The plan's "Vitest unit for API client login path" is covered
 * here rather than against `api.ts` directly because the login path is
 * what page-1-login owns.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { currentUser, getBearerToken, loadMe, login, logout, setBearerToken, setCurrentUser } from './auth';
import { agents, ensureAgentsLoaded, resetAgents } from './stores/agents';

type FetchArgs = [RequestInfo | URL, RequestInit | undefined];

function mockFetch(response: { status?: number; body?: unknown }): {
  fetchSpy: ReturnType<typeof vi.fn>;
  calls: () => FetchArgs[];
} {
  const status = response.status ?? 200;
  const body = response.body ?? null;
  const fetchSpy = vi.fn(async (..._args: unknown[]) => {
    return new Response(body === null ? null : JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  });
  (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
  return { fetchSpy, calls: () => fetchSpy.mock.calls as unknown as FetchArgs[] };
}

// `currentUser` is a module-level store, so test order can otherwise
// leak the previous test's value. Reset both halves of auth state
// (bearer + store) around every spec.
const FRESH_USER_STATE = Symbol('fresh');

describe('lib/auth', () => {
  beforeEach(() => {
    setBearerToken(null);
    resetAgents();
    // Mirror the "no /me has resolved yet" boot state by clearing the
    // store. We can't set `undefined` via the public API; set to null
    // (the "loaded, not authenticated" state) which is a safe default
    // for the negative assertions below.
    setCurrentUser(null);
  });
  afterEach(() => {
    setBearerToken(null);
    setCurrentUser(null);
    resetAgents();
    vi.restoreAllMocks();
  });
  // Quiet the linter — the symbol is only here to document intent.
  void FRESH_USER_STATE;

  describe('login()', () => {
    it('POSTs to /api/auth/login, stashes the bearer, updates currentUser', async () => {
      const { calls } = mockFetch({ body: { token: 'opaque-abc', user: { uid: 'alice' } } });

      const user = await login('alice');

      expect(user).toEqual({ uid: 'alice' });
      expect(getBearerToken()).toBe('opaque-abc');
      expect(get(currentUser)).toEqual({ uid: 'alice' });

      // Hit the right path with a JSON body.
      const [url, init] = calls()[0];
      expect(url).toBe('/api/auth/login');
      expect(init?.method).toBe('POST');
      expect(init?.body).toBe(JSON.stringify({ username: 'alice' }));
    });

    it('omits empty email from the payload', async () => {
      const { calls } = mockFetch({ body: { token: 't', user: { uid: 'alice' } } });
      await login('alice', '   ');
      expect(calls()[0][1]?.body).toBe(JSON.stringify({ username: 'alice' }));
    });

    it('includes a trimmed email when provided', async () => {
      const { calls } = mockFetch({ body: { token: 't', user: { uid: 'alice' } } });
      await login('alice', '  a@b.com  ');
      expect(calls()[0][1]?.body).toBe(JSON.stringify({ username: 'alice', email: 'a@b.com' }));
    });

    it('clears the cached agent roster when switching users', async () => {
      const fetchSpy = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
        const u = String(url);
        if (u === '/api/agents') {
          return new Response(
            JSON.stringify([{ slug: 'alice-agent', name: 'Alice Agent', description: '', created_at: null, last_active: null }]),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        if (u === '/api/auth/login' && init?.method === 'POST') {
          return new Response(JSON.stringify({ token: 'bob-token', user: { uid: 'bob' } }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({ detail: 'unexpected request' }), {
          status: 500,
          headers: { 'content-type': 'application/json' },
        });
      });
      (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

      setCurrentUser({ uid: 'alice' });
      await ensureAgentsLoaded();
      expect(get(agents).map((a) => a.slug)).toEqual(['alice-agent']);

      await login('bob');

      expect(get(currentUser)).toEqual({ uid: 'bob' });
      expect(get(agents)).toEqual([]);
    });

    it('propagates ApiError on non-2xx and leaves state untouched', async () => {
      mockFetch({ status: 422, body: { detail: 'username required' } });
      await expect(login('')).rejects.toMatchObject({ status: 422 });
      expect(getBearerToken()).toBeNull();
      // beforeEach reset currentUser to null; a failed login must not promote it.
      expect(get(currentUser)).toBeNull();
    });
  });

  describe('loadMe()', () => {
    it('returns the user on 200 and updates currentUser', async () => {
      mockFetch({ body: { uid: 'bob' } });
      const me = await loadMe();
      expect(me).toEqual({ uid: 'bob' });
      expect(get(currentUser)).toEqual({ uid: 'bob' });
    });

    it('returns null on 401 (no exception)', async () => {
      mockFetch({ status: 401, body: { detail: 'Not authenticated' } });
      const me = await loadMe();
      expect(me).toBeNull();
      expect(get(currentUser)).toBeNull();
    });

    it('clears the cached agent roster when /me resolves to a different user', async () => {
      const fetchSpy = vi.fn(async (url: RequestInfo | URL) => {
        const u = String(url);
        if (u === '/api/agents') {
          return new Response(
            JSON.stringify([{ slug: 'alice-agent', name: 'Alice Agent', description: '', created_at: null, last_active: null }]),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        if (u === '/api/auth/me') {
          return new Response(JSON.stringify({ uid: 'bob' }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({ detail: 'unexpected request' }), {
          status: 500,
          headers: { 'content-type': 'application/json' },
        });
      });
      (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

      setCurrentUser({ uid: 'alice' });
      await ensureAgentsLoaded();
      expect(get(agents).map((a) => a.slug)).toEqual(['alice-agent']);

      const me = await loadMe();

      expect(me).toEqual({ uid: 'bob' });
      expect(get(agents)).toEqual([]);
    });

    it('propagates non-401 errors', async () => {
      mockFetch({ status: 500, body: { detail: 'boom' } });
      await expect(loadMe()).rejects.toMatchObject({ status: 500 });
    });
  });

  describe('logout()', () => {
    it('POSTs /api/auth/logout, clears the bearer, resets the store', async () => {
      setBearerToken('stale');
      const { calls } = mockFetch({ body: { ok: true } });
      await logout();
      expect(calls()[0][0]).toBe('/api/auth/logout');
      expect(calls()[0][1]?.method).toBe('POST');
      expect(getBearerToken()).toBeNull();
      expect(get(currentUser)).toBeNull();
    });

    it('still clears local state when the network call fails', async () => {
      setBearerToken('stale');
      mockFetch({ status: 500, body: { detail: 'boom' } });
      await logout(); // must not throw
      expect(getBearerToken()).toBeNull();
      expect(get(currentUser)).toBeNull();
    });
  });
});
