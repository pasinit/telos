import './app.css';
import App from './App.svelte';
import { mount } from 'svelte';
import { registerSW } from 'virtual:pwa-register';
import { initTracing } from './lib/observability';

// OTel must initialise before any fetch() call so the
// FetchInstrumentation gets installed up-front. No-op when
// VITE_OTEL_EXPORTER_OTLP_ENDPOINT is unset.
initTracing();

// vite-plugin-pwa generates this virtual module. With `registerType:
// 'autoUpdate'` + `skipWaiting/clientsClaim` in vite.config.ts, a new
// SW activates as soon as it's installed and takes over open pages.
registerSW({ immediate: true });

const target = document.getElementById('app');
if (!target) {
  throw new Error("#app mount target missing from index.html — can't boot SPA");
}

// Svelte 5 mount API (classic syntax in components).
const app = mount(App, { target });
export default app;
