import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import { setBearerToken } from '../auth';
import {
  agents,
  createAgent,
  deleteAgent,
  ensureAgentsLoaded,
  refreshAgents,
  resetAgents,
} from './agents';

type FetchCall = [RequestInfo | URL, RequestInit | undefined];

function callsOf(spy: ReturnType<typeof vi.fn>): FetchCall[] {
  return spy.mock.calls as unknown as FetchCall[];
}

function mockJsonFetch(body: unknown, status = 200) {
  const spy = vi.fn(async (..._args: unknown[]) => {
    const res = new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
    return res;
  });
  (globalThis as unknown as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return { spy, calls: () => spy.mock.calls as unknown as FetchCall[] };
}

beforeEach(() => {
  setBearerToken(null);
  resetAgents();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetAgents();
});

describe('ensureAgentsLoaded', () => {
  it('fetches /api/agents once and caches the result', async () => {
    const { spy } = mockJsonFetch([
      { slug: 'a', name: 'A', description: 'x', created_at: null, last_active: null },
    ]);
    await ensureAgentsLoaded();
    await ensureAgentsLoaded();
    await ensureAgentsLoaded();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(callsOf(spy)[0][0]).toBe('/api/agents');
    expect(get(agents)).toHaveLength(1);
    expect(get(agents)[0].slug).toBe('a');
  });

  it('dedupes concurrent loads to one fetch', async () => {
    const { spy } = mockJsonFetch([]);
    await Promise.all([ensureAgentsLoaded(), ensureAgentsLoaded(), ensureAgentsLoaded()]);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('propagates errors and leaves the cache empty so the next call retries', async () => {
    const spy = vi.fn(async () => new Response('boom', { status: 500 }));
    (globalThis as unknown as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
    await expect(ensureAgentsLoaded()).rejects.toMatchObject({ status: 500 });

    // After the failure the next call must hit the network again.
    mockJsonFetch([]);
    await ensureAgentsLoaded();
  });
});

describe('refreshAgents', () => {
  it('re-fetches even when already loaded', async () => {
    const { spy } = mockJsonFetch([]);
    await ensureAgentsLoaded();
    await refreshAgents();
    expect(spy).toHaveBeenCalledTimes(2);
  });
});

describe('createAgent', () => {
  it('POSTs JSON, returns {slug,name}, and refreshes the store', async () => {
    const created = { slug: 'recipe-bot', name: 'Recipe Bot' };
    const fetchSpy = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (init?.method === 'POST' && u === '/api/agents') {
        return new Response(JSON.stringify(created), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      // refresh GET
      return new Response(
        JSON.stringify([{ slug: 'recipe-bot', name: 'Recipe Bot', description: 'd', created_at: null, last_active: null }]),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    });
    (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    const result = await createAgent('Recipe Bot', 'Suggests dinners');
    expect(result).toEqual(created);

    // First call: POST with JSON body
    const [, postInit] = fetchSpy.mock.calls[0] as unknown as FetchCall;
    expect(postInit?.method).toBe('POST');
    expect(postInit?.body).toBe(JSON.stringify({ name: 'Recipe Bot', description: 'Suggests dinners' }));
    expect(new Headers(postInit?.headers).get('Content-Type')).toBe('application/json');

    // Store was refreshed and exposes the new row.
    expect(get(agents)[0].slug).toBe('recipe-bot');
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('rethrows ApiError on 400 and does not refresh', async () => {
    const fetchSpy = vi.fn(async () =>
      new Response(JSON.stringify({ detail: 'Name and description are required' }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      }),
    );
    (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    await expect(createAgent('', '')).rejects.toMatchObject({ status: 400 });
    expect(fetchSpy).toHaveBeenCalledTimes(1); // no refresh attempted
  });
});

describe('deleteAgent', () => {
  it('DELETEs /api/agents/{slug} and refreshes', async () => {
    const fetchSpy = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
    });
    (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    await deleteAgent('cooking-coach');
    expect(callsOf(fetchSpy)[0][0]).toBe('/api/agents/cooking-coach');
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('URL-encodes slugs', async () => {
    const fetchSpy = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'content-type': 'application/json' } }));
    (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    await deleteAgent('weird slug/with chars');
    expect(callsOf(fetchSpy)[0][0]).toBe('/api/agents/weird%20slug%2Fwith%20chars');
  });
});
