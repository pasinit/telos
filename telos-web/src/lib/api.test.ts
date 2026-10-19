import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, apiGet, apiPost } from './api';
import { setBearerToken } from './auth';

type FetchArgs = [RequestInfo | URL, RequestInit | undefined];

function mockFetch(response: { status?: number; body?: unknown; contentType?: string }): {
  fetchSpy: ReturnType<typeof vi.fn>;
  calls: () => FetchArgs[];
} {
  const status = response.status ?? 200;
  const contentType = response.contentType ?? 'application/json';
  const body = response.body ?? null;
  const fetchSpy = vi.fn(async (..._args: unknown[]) => {
    const res = new Response(body === null ? null : typeof body === 'string' ? body : JSON.stringify(body), {
      status,
      headers: { 'content-type': contentType },
    });
    return res;
  });
  (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
  return { fetchSpy, calls: () => fetchSpy.mock.calls as unknown as FetchArgs[] };
}

describe('api()', () => {
  beforeEach(() => {
    setBearerToken(null);
  });
  afterEach(() => {
    setBearerToken(null);
    vi.restoreAllMocks();
  });

  it('prepends /api to bare paths', async () => {
    const { calls } = mockFetch({ body: { ok: true } });
    await apiGet('/auth/me');
    expect(calls()[0][0]).toBe('/api/auth/me');
  });

  it('does not double-prefix paths that already start with /api/', async () => {
    const { calls } = mockFetch({ body: { ok: true } });
    await apiGet('/api/usage');
    expect(calls()[0][0]).toBe('/api/usage');
  });

  it('sets Accept and Content-Type headers for JSON POST', async () => {
    const { calls } = mockFetch({ body: { ok: true } });
    await apiPost('/auth/login', { username: 'tomma' });
    const init = calls()[0][1]!;
    const headers = new Headers(init.headers);
    expect(headers.get('Accept')).toBe('application/json');
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(init.body).toBe(JSON.stringify({ username: 'tomma' }));
  });

  it('attaches Authorization header when a bearer token is set', async () => {
    setBearerToken('opaque-token');
    const { calls } = mockFetch({ body: { ok: true } });
    await apiGet('/auth/me');
    const init = calls()[0][1]!;
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer opaque-token');
  });

  it('sends credentials: same-origin so the auth cookie travels', async () => {
    const { calls } = mockFetch({ body: { ok: true } });
    await apiGet('/auth/me');
    expect(calls()[0][1]!.credentials).toBe('same-origin');
  });

  it('throws ApiError with status on non-2xx', async () => {
    mockFetch({ status: 401, body: { detail: 'unauthorized' } });
    await expect(api('/auth/me')).rejects.toBeInstanceOf(ApiError);
    await expect(api('/auth/me')).rejects.toMatchObject({ status: 401 });
  });

  it('does not set Content-Type when body is FormData', async () => {
    const { calls } = mockFetch({ body: { ok: true } });
    const fd = new FormData();
    fd.set('x', '1');
    await apiPost('/upload', fd);
    const headers = new Headers(calls()[0][1]!.headers);
    expect(headers.has('Content-Type')).toBe(false);
  });
});
