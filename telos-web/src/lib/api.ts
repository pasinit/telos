/**
 * Thin `fetch` wrapper for the Telos JSON API.
 *
 * Conventions:
 * - All app code calls these helpers — never raw `fetch` in components.
 * - Auth: same-origin cookie is sent automatically (`credentials: 'same-origin'`).
 *   When a Bearer token is set (PR 7D populates `lib/auth.ts`), it's
 *   attached via the `Authorization` header. The backend dependency
 *   (PR 7C) accepts either.
 * - Errors: 401 surfaces as an `ApiError` with `status: 401`; callers
 *   may handle by redirecting to `/login` via the auth store.
 * - JSON-only: this wrapper assumes JSON request/response bodies. For
 *   SSE streams, use `lib/sse.ts` (PR 7E).
 *
 * Shared file. Page implementers append helpers via DM to setup-impl.
 */

import { getBearerToken } from './auth';

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

type Json = string | number | boolean | null | Json[] | { [k: string]: Json };

export interface ApiOptions extends Omit<RequestInit, 'body'> {
  body?: Json | FormData | undefined;
  signal?: AbortSignal;
}

function buildHeaders(opts: ApiOptions): Headers {
  const headers = new Headers(opts.headers ?? {});
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  const isForm = opts.body instanceof FormData;
  if (opts.body !== undefined && !isForm && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const token = getBearerToken();
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  return headers;
}

async function parseBody(res: Response): Promise<unknown> {
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) {
    try {
      return await res.json();
    } catch {
      return null;
    }
  }
  // Empty / non-JSON: return text for diagnostics, null on empty.
  const text = await res.text();
  return text === '' ? null : text;
}

/**
 * Issue a request to `/api{path}` and return the parsed JSON body.
 *
 * `path` should start with `/` (e.g. `/auth/me`). The `/api` prefix is
 * added here so callers don't repeat it.
 */
export async function api<T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> {
  const url = path.startsWith('/api/') || path.startsWith('/api?') ? path : `/api${path}`;
  const init: RequestInit = {
    method: opts.method ?? 'GET',
    credentials: 'same-origin',
    headers: buildHeaders(opts),
    signal: opts.signal,
  };
  if (opts.body !== undefined) {
    init.body = opts.body instanceof FormData ? opts.body : JSON.stringify(opts.body);
  }
  const res = await fetch(url, init);
  const body = await parseBody(res);
  if (!res.ok) {
    throw new ApiError(res.status, body, typeof body === 'object' && body && 'detail' in body ? String((body as { detail: unknown }).detail) : undefined);
  }
  return body as T;
}

export const apiGet = <T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> => api<T>(path, { ...opts, method: 'GET' });
export const apiPost = <T = unknown>(path: string, body?: ApiOptions['body'], opts: ApiOptions = {}): Promise<T> =>
  api<T>(path, { ...opts, method: 'POST', body });
export const apiPut = <T = unknown>(path: string, body?: ApiOptions['body'], opts: ApiOptions = {}): Promise<T> =>
  api<T>(path, { ...opts, method: 'PUT', body });
export const apiDelete = <T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> => api<T>(path, { ...opts, method: 'DELETE' });
