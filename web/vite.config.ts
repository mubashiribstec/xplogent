import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies the API and WebSockets to the Xplogent backend on :8765.
// Keep this list in step with the backend's top-level routes (interfaces/api).
const API = [
  "/analytics", "/audit", "/auth", "/backup", "/branding", "/budget", "/config", "/docs", "/export", "/guide",
  "/health", "/import", "/memory", "/models", "/persona", "/providers", "/restore", "/roles", "/run", "/secrets",
  "/sessions", "/skills", "/status", "/telephony", "/tools", "/ui", "/voip",
];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      ...Object.fromEntries(API.map((p) => [p, { target: "http://localhost:8765", ws: true }])),
      "/ws": { target: "ws://localhost:8765", ws: true },
    },
  },
  build: {
    target: "es2020",
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        // React and the icon set change rarely: their own cached chunks.
        manualChunks: {
          react: ["react", "react-dom"],
          markdown: ["react-markdown", "remark-gfm"],
        },
      },
    },
  },
});
