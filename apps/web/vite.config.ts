import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the browser talks to the Vite origin; /api and /health are proxied to the
// control-plane API (default local-memory dev server on :8787).
export default defineConfig(({ mode }) => {
  const target = loadEnv(mode, ".", "").VITE_API_PROXY || "http://127.0.0.1:8787";
  return {
  plugins: [react()],
  server: {
    port: 5173,
    fs: { allow: ["../.."] }, // lets the UI import the committed contract fixture
    proxy: { "/api": target, "/health": target },
  },
  test: { environment: "node" },
  };
});
