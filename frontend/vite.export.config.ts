import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The offline export's viewer (10): the same reading view as the app, built from export.html into
// dist-export/, which the backend copies into every static ZIP (backend/app/jobs/exports.py). Relative
// paths, so the copy works from any folder; no public/ files beyond the codehover licence notice.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  publicDir: false,
  build: {
    outDir: "dist-export",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    rollupOptions: { input: "export.html" },
  },
});
