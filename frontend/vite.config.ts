/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Only VITE_API_BASE is read by the app (scripts/check-env-usage.mjs enforces the allowlist).
// Provider secrets live on the server and never enter the bundle (08, 22).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
    // The XSS tests load the offline export's viewer, which the backend ships in every ZIP.
    fs: { allow: [".", "../backend/app/engine/export_assets"] },
  },
  build: { sourcemap: false, target: "es2022" },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"], // e2e/ belongs to Playwright
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
