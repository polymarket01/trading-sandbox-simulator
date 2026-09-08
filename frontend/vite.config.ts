import { defineConfig } from "vite";
import { retainPageAssets } from "./retain-page-assets.mjs";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Existing browser tabs still import their original content-hashed chunks.
  build: { emptyOutDir: false },
  plugins: [react(), retainPageAssets()],
  server: {
    port: 5173,
    proxy: {
      "/liquidity-map": { target: "http://127.0.0.1:5174", ws: true, changeOrigin: true },
      "/api": { target: "http://127.0.0.1:5174", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:5174", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:5174", ws: true, changeOrigin: true },
    },
  },
});
