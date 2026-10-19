import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import { createNightlyStore, type NightlyPayload } from './nightly';

function mockFetch(response: { status?: number; body?: unknown }): ReturnType<typeof vi.fn> {
  const status = response.status ?? 200;
  const fetchSpy = vi.fn(async (..._args: unknown[]) =>
    new Response(response.body === undefined ? null : JSON.stringify(response.body), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  );
  (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
  return fetchSpy;
}

const PAYLOAD: NightlyPayload = {
  slug: 'jobber',
  feedback: [
    {
      id: 2, run_id: 5, user_id: 'dana', slug: 'jobber', status: 'completed', error: null,
      feedback: {
        score: 4, summary: 'solid', failures: [], prompt_suggestions: ['be specific'],
        tool_use_issues: [],
        recall_misses: [{ turn_id: 't1', query_used: 'London roles', classification: 'ranking_gap', detail: '' }],
      },
    },
  ],
};

describe('createNightlyStore', () => {
  afterEach(() => vi.restoreAllMocks());

  it('fetches the per-agent nightly endpoint', async () => {
    const fetchSpy = mockFetch({ body: PAYLOAD });
    const store = createNightlyStore('jobber');
    await store.refresh();
    expect(fetchSpy.mock.calls[0][0]).toBe('/api/agents/jobber/nightly');
    const state = get(store);
    expect(state.loading).toBe(false);
    expect(state.error).toBeNull();
    expect(state.data?.feedback[0].feedback?.score).toBe(4);
    expect(state.data?.feedback[0].feedback?.recall_misses[0].classification).toBe('ranking_gap');
  });

  it('surfaces an error without throwing', async () => {
    mockFetch({ status: 500, body: { error: 'boom' } });
    const store = createNightlyStore('jobber');
    await store.refresh();
    const state = get(store);
    expect(state.error).not.toBeNull();
    expect(state.data).toBeNull();
  });

  it('url-encodes the slug', async () => {
    const fetchSpy = mockFetch({ body: PAYLOAD });
    const store = createNightlyStore('_housekeeper');
    await store.refresh();
    expect(fetchSpy.mock.calls[0][0]).toBe('/api/agents/_housekeeper/nightly');
  });
});
