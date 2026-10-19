/// <reference types="svelte" />
/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

interface ImportMetaEnv {
  /** OTLP HTTP endpoint for SPA-side OTel traces. Empty / unset = no-op.
   *  Local dev with `make aspire`: `http://localhost:4318/v1/traces`.
   *  Production should prefer a same-origin proxy path like `/otlp/v1/traces`. */
  readonly VITE_OTEL_EXPORTER_OTLP_ENDPOINT?: string;
  /** Service name reported on SPA spans. Defaults to "telos-web". */
  readonly VITE_OTEL_SERVICE_NAME?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
