import { defineConfig } from "@playwright/test";

// The critical journey (13) against a test-only server (backend/tests/e2e/server.py): the real API on
// the integration-test database with fake Google and GitHub providers, serving the built frontend.
// Needs TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL like the backend integration tests.
const PORT = Number(process.env.E2E_PORT ?? 8765);

export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    // Installed Google Chrome; set PW_CHANNEL="" to use Playwright's bundled Chromium instead.
    ...(process.env.PW_CHANNEL === "" ? {} : { channel: process.env.PW_CHANNEL ?? "chrome" }),
    trace: "retain-on-failure",
    // PW_COLOR_SCHEME=dark repeats the journey (and its axe checks) in the dark theme.
    colorScheme: process.env.PW_COLOR_SCHEME === "dark" ? "dark" : "light",
  },
  webServer: {
    command: `npm run build && cd ../backend && .venv/bin/python -m tests.e2e.server --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/v1/health/live`,
    timeout: 180_000,
    reuseExistingServer: false,
    stdout: "pipe",
  },
});
