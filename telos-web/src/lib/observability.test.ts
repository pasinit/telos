/**
 * Smoke tests for the SPA OTel module.
 *
 * The real exporter path only fires when
 * `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` is set at build time, so these
 * tests cover the no-op path and the public helpers that don't require
 * a wired provider (span recording is covered by `@opentelemetry/sdk-trace-web`
 * upstream).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadObservability(endpoint?: string) {
  vi.resetModules();
  vi.unstubAllEnvs();
  if (endpoint !== undefined) {
    vi.stubEnv('VITE_OTEL_EXPORTER_OTLP_ENDPOINT', endpoint);
  }
  return import('./observability');
}

async function loadTracingWithMockedFetch(endpoint: string) {
  const registerInstrumentations = vi.fn();
  let fetchConfig: { propagateTraceHeaderCorsUrls: (string | RegExp)[] } | null = null;

  vi.doMock('@opentelemetry/instrumentation', () => ({ registerInstrumentations }));
  vi.doMock('@opentelemetry/instrumentation-fetch', () => ({
    FetchInstrumentation: class {
      constructor(config: { propagateTraceHeaderCorsUrls: (string | RegExp)[] }) {
        fetchConfig = config;
      }
    },
  }));
  vi.doMock('@opentelemetry/sdk-trace-web', () => ({
    BatchSpanProcessor: class {
      constructor(public exporter: unknown) {}
    },
    WebTracerProvider: class {
      register = vi.fn();
    },
  }));

  const { initTracing } = await loadObservability(endpoint);

  return {
    initTracing,
    registerInstrumentations,
    targets: () => {
      expect(fetchConfig).not.toBeNull();
      return fetchConfig!.propagateTraceHeaderCorsUrls;
    },
  };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.doUnmock('@opentelemetry/instrumentation');
  vi.doUnmock('@opentelemetry/instrumentation-fetch');
  vi.doUnmock('@opentelemetry/sdk-trace-web');
  vi.restoreAllMocks();
});

describe('observability', () => {
  it('initTracing is idempotent and a no-op when endpoint is unset', async () => {
    // Vite test env doesn't set VITE_OTEL_EXPORTER_OTLP_ENDPOINT.
    const { initTracing } = await loadObservability();
    expect(initTracing()).toBe(false);
    expect(initTracing()).toBe(false);
  });

  it('tracer() returns a usable tracer even without init', async () => {
    const { tracer } = await loadObservability();
    const t = tracer();
    expect(typeof t.startActiveSpan).toBe('function');
  });

  it('withSpan runs the callback and forwards the return value', async () => {
    const { withSpan } = await loadObservability();
    const result = await withSpan('test.smoke', { flag: true, count: 1 }, async () => 42);
    expect(result).toBe(42);
  });

  it('withSpan records exceptions and rethrows', async () => {
    const { withSpan } = await loadObservability();
    await expect(
      withSpan('test.boom', {}, async () => {
        throw new Error('kaboom');
      }),
    ).rejects.toThrow('kaboom');
  });

  it('currentTraceId is null outside a span', async () => {
    const { currentTraceId } = await loadObservability();
    expect(currentTraceId()).toBeNull();
  });

  it('does not expose the no-op tracer all-zero trace id', async () => {
    const { traceIdFromSpan, withSpan } = await loadObservability();
    await withSpan('test.noop', {}, async (sp) => {
      expect(sp.spanContext().traceId).toBe('00000000000000000000000000000000');
      expect(traceIdFromSpan(sp)).toBeNull();
    });
  });

  it('registers fetch instrumentation with restricted propagation targets', async () => {
    const { initTracing, registerInstrumentations, targets } =
      await loadTracingWithMockedFetch('http://localhost:4318/v1/traces');

    expect(initTracing()).toBe(true);
    expect(initTracing()).toBe(true);
    expect(registerInstrumentations).toHaveBeenCalledOnce();
    const propagationTargets = targets();
    expect(propagationTargets).toContain('http://localhost:4318/v1/traces');
    expect(propagationTargets.some((target) => target instanceof RegExp && target.test('/agents/demo/chat'))).toBe(true);
    expect(propagationTargets.some((target) => target instanceof RegExp && target.test('/api/agents'))).toBe(true);
    expect(propagationTargets.some((target) => target instanceof RegExp && target.test('/profile'))).toBe(false);
    expect(propagationTargets.some((target) => target instanceof RegExp && target.test('https://third-party.example/api'))).toBe(false);
    expect(propagationTargets).not.toContain('http://localhost:4318');
    expect(propagationTargets).not.toContain('http://localhost:4318/metrics');
  });

  it('keeps a production same-origin OTLP proxy endpoint exact', async () => {
    const { initTracing, targets } = await loadTracingWithMockedFetch('/otlp/v1/traces');

    expect(initTracing()).toBe(true);
    const propagationTargets = targets();
    expect(propagationTargets).toContain('/otlp/v1/traces');
    expect(propagationTargets).not.toContain('/otlp');
    expect(propagationTargets).not.toContain('/otlp/metrics');
  });
});
