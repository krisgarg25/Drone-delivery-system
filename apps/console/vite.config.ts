import { defineConfig } from "vite";

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
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
