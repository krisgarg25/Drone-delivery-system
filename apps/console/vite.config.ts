import { defineConfig } from "vite";

/**
 * Dev and build config for the operations console.
 *
 * The `/health`, `/version` and `/v1` proxies point at the local control plane
 * (`make api`, port 8000). There is no tile-server proxy and there must not be one
 * without a licence: the basemap is drawn from the frozen synthetic bundle instead, and
 * the CSP in nginx.conf only allows `img-src 'self' data:`.
 */
export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/version": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000",
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/version": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000",
    },
  },
  resolve: {
    // deck.gl layers come from three packages that must share one @deck.gl/core instance.
    dedupe: ["@deck.gl/core", "@luma.gl/engine", "@luma.gl/core"],
  },
  optimizeDeps: {
    include: ["maplibre-gl", "@deck.gl/core", "@deck.gl/layers", "@deck.gl/mapbox"],
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2023",
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      // MapLibre and deck.gl are the whole weight of this app; naming the chunks keeps a
      // later panel addition from being blamed for the size.
      output: {
        manualChunks: {
          maplibre: ["maplibre-gl"],
          deck: ["@deck.gl/core", "@deck.gl/layers", "@deck.gl/mapbox"],
        },
      },
    },
  },
});
