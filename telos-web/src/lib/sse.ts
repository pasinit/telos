/**
 * SSE-over-POST streaming helper.
 *
 * The chat endpoint (`POST /agents/{slug}/chat`) returns
 * ``text/event-stream`` over a POST body. `EventSource` only does GET,
 * so we use `fetch` + a `ReadableStream` reader, then parse SSE frames
 * by hand (small — see ``parseSseChunk`` below).
 *
 * Conventions:
 * - Pass an `AbortSignal` (`AbortController.signal`) so the stream
 *   tears down on unmount/navigation. Without it the server keeps
 *   running, billing the user, until the agent finishes.
 * - The Bearer token from `lib/auth.ts` is attached automatically.
 * - SSE frames are delimited by `\n\n`. Each frame may have multiple
 *   `field: value\n` lines; we only consume `data:` lines. One JSON
 *   object per `data:` line per the server contract.
 * - UTF-8: `TextDecoder({stream: true})` so multi-byte codepoints
 *   split across chunk boundaries don't garble (emoji, CJK).
 */

import { getBearerToken } from './auth';
import { currentTraceparent } from './observability';

export interface StreamSseOptions<F> {
  /** Optional JSON request body. */
  body?: unknown;
  /** Called once per parsed SSE frame. */
  onFrame: (frame: F) => void;
  /** Caller-owned controller. Required for clean teardown. */
  signal: AbortSignal;
  /** Override HTTP method. Defaults to POST. */
  method?: string;
}

function buildHeaders(): Headers {
  const headers = new Headers({
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  });
  const token = getBearerToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const traceparent = currentTraceparent();
  if (traceparent) headers.set('traceparent', traceparent);
  return headers;
}

function resolveUrl(path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://')) return path;
  if (path.startsWith('/api/') || path.startsWith('/agents/')) return path;
  return `/api${path}`;
}

/**
 * Parse one chunk of SSE bytes accumulated so far. Returns the JSON
 * payloads from complete frames and the leftover buffer (incomplete
 * trailing frame). Exported for unit tests; consumers should use
 * `streamSSE`.
 */
export function parseSseChunk(buffer: string): { frames: unknown[]; rest: string } {
  // SSE event delimiter is a blank line — `\n\n` (the spec allows
  // `\r\n\r\n` but FastAPI emits `\n\n` only).
  const parts = buffer.split('\n\n');
  const rest = parts.pop() ?? '';
  const frames: unknown[] = [];
  for (const frame of parts) {
    for (const line of frame.split('\n')) {
      if (!line.startsWith('data:')) continue;
      // Spec says strip one leading space after the colon.
      const data = line.startsWith('data: ') ? line.slice(6) : line.slice(5);
      if (!data) continue;
      try {
        frames.push(JSON.parse(data));
      } catch {
        // Server contract is one JSON object per `data:` line. A parse
        // failure shouldn't kill the whole stream — skip the bad frame.
      }
    }
  }
  return { frames, rest };
}

/**
 * Open an SSE channel and invoke `onFrame` for each parsed JSON frame.
 *
 * Resolves when the stream ends (server closed `data:` queue) or
 * rejects with `AbortError` when `signal` is aborted. Other failures
 * (HTTP non-2xx, network drop, missing response body) reject with a
 * generic `Error` — callers should distinguish via `err.name`.
 */
export async function streamSSE<F>(path: string, opts: StreamSseOptions<F>): Promise<void> {
  const init: RequestInit = {
    method: opts.method ?? 'POST',
    headers: buildHeaders(),
    credentials: 'same-origin',
    signal: opts.signal,
  };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
  }

  const resp = await fetch(resolveUrl(path), init);
  if (!resp.ok) {
    throw new Error(`SSE HTTP ${resp.status}`);
  }
  if (!resp.body) {
    // Some proxies / service workers strip the body. We can't recover.
    throw new Error('SSE response has no body — proxy buffering?');
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // `stream: true` keeps partial UTF-8 codepoints buffered.
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSseChunk(buffer);
      buffer = parsed.rest;
      for (const frame of parsed.frames) {
        opts.onFrame(frame as F);
      }
    }
    // Flush any remaining buffered bytes (final chunk).
    buffer += decoder.decode();
    if (buffer.trim()) {
      const parsed = parseSseChunk(buffer + '\n\n');
      for (const frame of parsed.frames) {
        opts.onFrame(frame as F);
      }
    }
  } finally {
    // `releaseLock()` is implied by `cancel()`. Swallow the rejection
    // when the stream was already finished — `cancel()` is idempotent.
    try {
      await reader.cancel();
    } catch {
      /* idempotent */
    }
  }
}
