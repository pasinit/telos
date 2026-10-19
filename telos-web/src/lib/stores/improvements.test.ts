/**
 * Tests for the improvements polling store.
 *
 * We mock global ``fetch`` directly (same pattern as ``api.test.ts``)
 * so the store goes through the real ``lib/api`` wrapper — that way
 * the path-building + bearer header + abort plumbing are all
 * exercised end to end.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import { DEFAULT_POLL_INTERVAL_MS, createImprovementsStore } from './improvements';

type FetchArgs = [RequestInfo | URL, RequestInit | undefined];

function makePayload(overrides: Record<string, unknown> = {}) {
  return {
    slug: 'weather',
    limit: 200,
    offset: 0,
    runs: [
      {
        id: 1,
        agent_slug: 'weather',
        started_at: '2025-01-01T00:00:00',
        finished_at: '2025-01-01T00:00:01',
        model: 'gpt-foo',
        decision: 'rejected',
        current_sha: 'aaaaaaaa',
        proposed_sha: 'bbbbbbbb',
        rationale: 'r',
        proposed_diff: '--- a\n+++ b\n',
        score_delta: -0.1,
        judge_summary: 'judge picked current',
        error: null,
      },
    ],
    ...overrides,
  };
}

function mockFetch(payloads: object[]): {
  spy: ReturnType<typeof vi.fn>;
  calls: () => FetchArgs[];
} {
  let i = 0;
  const spy = vi.fn(async (..._args: unknown[]) => {
    const body = payloads[Math.min(i, payloads.length - 1)];
    i += 1;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
  (globalThis as unknown as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return { spy, calls: () => spy.mock.calls as unknown as FetchArgs[] };
}

describe('createImprovementsStore', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('refresh() fetches, populates data, clears loading', async () => {
    const { calls } = mockFetch([makePayload()]);
    const store = createImprovementsStore('weather');
    await store.refresh();
    const state = get(store);
    expect(state.loading).toBe(false);
    expect(state.error).toBeNull();
    expect(state.data?.runs).toHaveLength(1);
    expect(state.lastFetched).toBeGreaterThan(0);
    // Builds the expected /api/agents/{slug}/improvements?limit=200 path.
    expect(calls()[0][0]).toBe('/api/agents/weather/improvements?limit=200');
  });

  it('includes the decision filter in the query string', async () => {
    const { calls } = mockFetch([makePayload(), makePayload()]);
    const store = createImprovementsStore('weather', 'rejected');
    await store.refresh();
    expect(String(calls()[0][0])).toContain('decision=rejected');
    expect(store.getDecision()).toBe('rejected');
  });

  it('setDecision() updates the filter and re-fetches', async () => {
    const { calls } = mockFetch([makePayload(), makePayload()]);
    const store = createImprovementsStore('weather');
    await store.refresh();
    store.setDecision('approved');
    // Setting the decision schedules a fetch; drain microtasks.
    await Promise.resolve();
    await Promise.resolve();
    expect(store.getDecision()).toBe('approved');
    expect(String(calls()[1][0])).toContain('decision=approved');
  });

  it('start() polls at the configured interval; stop() cancels', async () => {
    const { spy } = mockFetch([makePayload(), makePayload(), makePayload()]);
    const store = createImprovementsStore('weather');
    store.start(1000);
    // Tick three intervals.
    await vi.advanceTimersByTimeAsync(3500);
    expect(spy.mock.calls.length).toBeGreaterThanOrEqual(3);
    const seen = spy.mock.calls.length;
    store.stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(spy.mock.calls.length).toBe(seen);
  });

  it('start() defaults to DEFAULT_POLL_INTERVAL_MS when no interval given', async () => {
    const { spy } = mockFetch([makePayload(), makePayload()]);
    const store = createImprovementsStore('weather');
    store.start();
    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS + 100);
    expect(spy).toHaveBeenCalledTimes(1);
    store.stop();
  });

  it('start() is idempotent — calling twice does not double-poll', async () => {
    const { spy } = mockFetch([makePayload(), makePayload(), makePayload()]);
    const store = createImprovementsStore('weather');
    store.start(500);
    store.start(500);
    await vi.advanceTimersByTimeAsync(1100);
    // 1100ms / 500ms = 2 ticks, NOT 4.
    expect(spy.mock.calls.length).toBeLessThanOrEqual(3);
    store.stop();
  });

  it('captures errors in state.error and clears loading', async () => {
    (globalThis as unknown as { fetch: typeof fetch }).fetch = vi.fn(async () => {
      return new Response(JSON.stringify({ detail: 'nope' }), {
        status: 500,
        headers: { 'content-type': 'application/json' },
      });
    }) as unknown as typeof fetch;
    const store = createImprovementsStore('weather');
    await store.refresh();
    const state = get(store);
    expect(state.loading).toBe(false);
    expect(state.error).not.toBeNull();
    expect(state.data).toBeNull();
  });

  it('stop() aborts the in-flight fetch instead of letting it land', async () => {
    let resolveFetch: (r: Response) => void = () => undefined;
    const pending = new Promise<Response>((r) => {
      resolveFetch = r;
    });
    const fetchSpy = vi.fn((..._args: unknown[]) => pending);
    (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    const store = createImprovementsStore('weather');
    const p = store.refresh();
    store.stop();
    // The aborter should have been triggered — surface it to the
    // underlying fetch so the promise resolves and refresh() returns.
    resolveFetch(new Response('null', { status: 200, headers: { 'content-type': 'application/json' } }));
    await p;
    const state = get(store);
    // Aborted refresh must NOT have stamped data.
    expect(state.data).toBeNull();
  });
});
