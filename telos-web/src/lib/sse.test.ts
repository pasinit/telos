import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { context, trace, TraceFlags, type Span } from '@opentelemetry/api';
import { ZoneContextManager } from '@opentelemetry/context-zone';
import { parseSseChunk, streamSSE } from './sse';

beforeEach(() => {
  context.disable();
});

afterEach(() => {
  context.disable();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('parseSseChunk', () => {
  it('returns an empty result for an empty buffer', () => {
    expect(parseSseChunk('')).toEqual({ frames: [], rest: '' });
  });

  it('keeps an incomplete trailing frame in the rest buffer', () => {
    // No double-newline yet → the whole buffer is "rest".
    const r = parseSseChunk('data: {"text"');
    expect(r.frames).toEqual([]);
    expect(r.rest).toBe('data: {"text"');
  });

  it('parses a single complete frame', () => {
    const r = parseSseChunk('data: {"text":"hi"}\n\n');
    expect(r.frames).toEqual([{ text: 'hi' }]);
    expect(r.rest).toBe('');
  });

  it('parses multiple frames in a single buffer', () => {
    const r = parseSseChunk('data: {"text":"a"}\n\ndata: {"text":"b"}\n\n');
    expect(r.frames).toEqual([{ text: 'a' }, { text: 'b' }]);
    expect(r.rest).toBe('');
  });

  it('leaves the incomplete trailing frame in `rest` while emitting completed ones', () => {
    const r = parseSseChunk('data: {"text":"a"}\n\ndata: {"text":"b"');
    expect(r.frames).toEqual([{ text: 'a' }]);
    expect(r.rest).toBe('data: {"text":"b"');
  });

  it('ignores non-data lines (comment / event names)', () => {
    const r = parseSseChunk(': keepalive\nevent: ping\ndata: {"usage":{"prompt_tokens":1}}\n\n');
    expect(r.frames).toEqual([{ usage: { prompt_tokens: 1 } }]);
  });

  it('skips a malformed JSON line without dropping subsequent valid ones', () => {
    const r = parseSseChunk('data: {not json}\n\ndata: {"text":"ok"}\n\n');
    expect(r.frames).toEqual([{ text: 'ok' }]);
  });

  it('handles data: with no leading space (spec-permissive)', () => {
    const r = parseSseChunk('data:{"text":"strict"}\n\n');
    expect(r.frames).toEqual([{ text: 'strict' }]);
  });

  it('keeps partial multi-byte safe boundary via separate buffer accumulation', () => {
    // Simulate split chunk: first call ends mid-frame, second completes.
    const first = parseSseChunk('data: {"text":"hel');
    expect(first.frames).toEqual([]);
    const next = parseSseChunk(first.rest + 'lo"}\n\n');
    expect(next.frames).toEqual([{ text: 'hello' }]);
  });
});

describe('streamSSE', () => {
  it('sends the active traceparent header with the streaming request', async () => {
    const traceparent = '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01';
    context.setGlobalContextManager(new ZoneContextManager().enable());
    const activeSpan = {
      spanContext: () => ({
        traceId: '0123456789abcdef0123456789abcdef',
        spanId: '0123456789abcdef',
        traceFlags: TraceFlags.SAMPLED,
      }),
    } as Span;
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"text":"ok"}\n\n'));
        controller.close();
      },
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const frames: unknown[] = [];
    await context.with(trace.setSpan(context.active(), activeSpan), async () => {
      await streamSSE('/agents/demo/chat', {
        body: { message: 'hello' },
        onFrame: (frame) => frames.push(frame),
        signal: new AbortController().signal,
      });
    });

    expect(frames).toEqual([{ text: 'ok' }]);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.headers).toBeInstanceOf(Headers);
    expect((init.headers as Headers).get('traceparent')).toBe(traceparent);
  });
});
