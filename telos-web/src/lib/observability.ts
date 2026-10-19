/**
 * OpenTelemetry tracing for the SPA.
 *
 * Mirror of `telos/observability.py` on the frontend side. When
 * `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` is set at build time, `initTracing`
 * wires a WebTracerProvider with an OTLP HTTP exporter and auto-spans
 * fetch instrumentation. Telos API fetches propagate `traceparent` to
 * the backend automatically. When the env var is absent / empty, init is
 * a no-op — `span()` still works but never exports anywhere.
 *
 * In dev (`make aspire` + `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`
 * for the backend), build the SPA with
 * `VITE_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces`.
 * Production should use a controlled same-origin collector/proxy path such as
 * `/otlp/v1/traces`, not broad collector CORS.
 *
 * The root span on every chat turn is created by `withSpan` in
 * Chat.svelte — when the SPA originates the trace, the backend's
 * FastAPIInstrumentor inherits it via the propagated `traceparent` and
 * the whole tree shows up in Jaeger rooted at the UI side.
 */

import {
  isSpanContextValid,
  SpanStatusCode,
  trace,
  type Span,
  type Tracer,
} from '@opentelemetry/api';
import { ZoneContextManager } from '@opentelemetry/context-zone';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { WebTracerProvider, BatchSpanProcessor } from '@opentelemetry/sdk-trace-web';
import { ATTR_SERVICE_NAME } from '@opentelemetry/semantic-conventions';

const TRACER_NAME = 'telos-web';

let tracingEnabled = false;

/**
 * Initialise tracing once. Returns true when a real exporter was
 * wired, false on the no-op path. Idempotent — repeat calls return
 * the same decision without re-wiring.
 */
export function initTracing(): boolean {
  if (tracingEnabled) return true;

  const endpoint = (import.meta.env.VITE_OTEL_EXPORTER_OTLP_ENDPOINT ?? '').trim();
  if (!endpoint) {
    return false;
  }

  const serviceName = (import.meta.env.VITE_OTEL_SERVICE_NAME ?? '').trim() || 'telos-web';
  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({
      [ATTR_SERVICE_NAME]: serviceName,
    }),
    spanProcessors: [
      new BatchSpanProcessor(new OTLPTraceExporter({ url: endpoint })),
    ],
  });

  // ZoneContextManager keeps span context propagating across async
  // boundaries in the browser (where there's no Python-style contextvar).
  provider.register({ contextManager: new ZoneContextManager() });

  // Auto-instrument fetch and restrict W3C trace-header propagation to Telos
  // API requests and the configured OTLP endpoint only.
  registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [
      new FetchInstrumentation({
        propagateTraceHeaderCorsUrls: tracePropagationTargets(endpoint),
      }),
    ],
  });

  tracingEnabled = true;
  return true;
}

export function tracer(): Tracer {
  return trace.getTracer(TRACER_NAME);
}

/**
 * Run `fn` inside a span named `name`. Span attributes come from
 * `attrs` (only primitive types). Exceptions are recorded on the span
 * and re-raised. Returns whatever `fn` returns.
 */
export async function withSpan<T>(
  name: string,
  attrs: Record<string, string | number | boolean | undefined>,
  fn: (span: Span) => Promise<T>,
): Promise<T> {
  return tracer().startActiveSpan(name, async (sp) => {
    for (const [k, v] of Object.entries(attrs)) {
      if (v !== undefined) sp.setAttribute(k, v);
    }
    try {
      return await fn(sp);
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      sp.recordException(error);
      sp.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw err;
    } finally {
      sp.end();
    }
  });
}

/**
 * Active span's trace_id as a 32-char hex string, or null when no
 * active span. Useful for surfacing a "request ID" in the UI footer
 * / error envelopes so users can quote it when reporting issues.
 */
export function currentTraceId(): string | null {
  const sp = trace.getActiveSpan();
  if (!sp) return null;
  return traceIdFromSpan(sp);
}

export function traceIdFromSpan(sp: Span): string | null {
  const ctx = sp.spanContext();
  return isSpanContextValid(ctx) ? ctx.traceId : null;
}

export function currentTraceparent(): string | null {
  const sp = trace.getActiveSpan();
  if (!sp) return null;
  return traceparentFromSpan(sp);
}

export function traceparentFromSpan(sp: Span): string | null {
  const ctx = sp.spanContext();
  if (!isSpanContextValid(ctx)) return null;
  const flags = ctx.traceFlags.toString(16).padStart(2, '0');
  return `00-${ctx.traceId}-${ctx.spanId}-${flags}`;
}

function tracePropagationTargets(endpoint: string): (string | RegExp)[] {
  const targets: (string | RegExp)[] = [/^\/(?:api|agents)(?:\/|$)/];
  if (typeof window !== 'undefined' && window.location?.origin) {
    targets.push(new RegExp(`^${escapeRegExp(window.location.origin)}/(?:api|agents)(?:/|$)`));
  }
  if (endpoint) targets.push(endpoint);
  return targets;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
