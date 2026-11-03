import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev server proxies the API to the gateway, so the browser talks to one origin (no CORS).
const gateway = "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/v1": gateway, "/.well-known": gateway },
  },
  build: { target: "es2023", sourcemap: false },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
