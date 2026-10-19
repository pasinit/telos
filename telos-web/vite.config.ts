import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { VitePWA } from 'vite-plugin-pwa';

// Backend dev server.
const API_TARGET = process.env.TELOS_API_URL ?? 'http://127.0.0.1:7433';

export default defineConfig({
  plugins: [
    svelte(),
    VitePWA({
      registerType: 'autoUpdate',
      strategies: 'generateSW',
      injectRegister: 'auto',
      manifest: {
        name: 'Telos',
        short_name: 'Telos',
        description: 'Your personal AI agents',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        background_color: '#030712',
        theme_color: '#1a1a2e',
        icons: [
          { src: '/icon-dark.svg', sizes: 'any', type: 'image/svg+xml' },
          { src: '/apple-touch-icon-dark.png', sizes: '180x180', type: 'image/png' },
        ],
      },
      workbox: {
        navigateFallback: '/index.html',
        // History-mode SPA: every non-asset path must fall through to
        // index.html EXCEPT the FastAPI-owned namespaces.
        navigateFallbackDenylist: [
          /^\/api\//,
          // Recordatio dashboard (memories/costs/feedback) — reverse-proxied by
          // Caddy at /recordatio/*, NOT part of the SPA. Without this the
          // installed service worker rewrites /recordatio/* navigations to
          // index.html, so on mobile the links just showed the Telos chat.
          /^\/recordatio(?:\/|$)/,
          /^\/oauth\//,
          /^\/docs/,
          /^\/openapi\.json/,
          /^\/healthz/,
          /^\/otel(?:\/|$)/,
          /^\/structuredlogs(?:\/|$)/,
          /^\/traces(?:\/|$)/,
          /^\/metrics(?:\/|$)/,
          /^\/resources(?:\/|$)/,
          /^\/consolelogs(?:\/|$)/,
          /^\/logs(?:\/|$)/,
          /^\/exceptions(?:\/|$)/,
          /^\/_blazor(?:\/|$)/,
          /^\/_content(?:\/|$)/,
          /^\/framework(?:\/|$)/,
          /^\/css(?:\/|$)/,
          /^\/js(?:\/|$)/,
          /^\/Aspire\.Dashboard\.styles\.css$/,
        ],
        globPatterns: ['**/*.{js,css,html,svg,png,ico,woff2}'],
        runtimeCaching: [
          {
            // JSON is user-specific; never cache it across users.
            urlPattern: ({ url }) => url.pathname.startsWith('/api/'),
            handler: 'NetworkOnly',
          },
          {
            // SSE chat stream — must never be cached.
            urlPattern: ({ url }) =>
              url.pathname.startsWith('/agents/') && url.pathname.endsWith('/chat'),
            handler: 'NetworkOnly',
          },
          {
            urlPattern: /^https:\/\/fonts\.(googleapis|gstatic)\.com\//,
            handler: 'StaleWhileRevalidate',
            options: { cacheName: 'google-fonts' },
          },
        ],
        skipWaiting: true,
        clientsClaim: true,
      },
    }),
  ],
  server: {
    port: 5173,
    proxy: {
      // Backend-owned namespaces proxy to FastAPI in dev. The SPA
      // serves everything else.
      '/api': { target: API_TARGET, changeOrigin: true },
      '/oauth': { target: API_TARGET, changeOrigin: true },
      '/healthz': { target: API_TARGET, changeOrigin: true },
      '/agents': {
        // The chat SSE endpoint is /agents/{slug}/chat. Proxy it for
        // dev so the EventStream works end-to-end.
        target: API_TARGET,
        changeOrigin: true,
        // Only intercept the chat SSE — the rest of /agents is SPA.
        bypass: (req) => {
          if (req.url && /^\/agents\/[^/]+\/chat$/.test(req.url)) return undefined;
          return req.url;
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    target: 'es2022',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.{test,spec}.ts'],
  },
});
