import { defineConfig } from "@playwright/test";

/**
 * Console audit specs. Servers are expected to be already running (preview on :4173,
 * dev on :5173, API on :8000) exactly like the pytest e2e gate; nothing is spawned here
 * so the specs audit the same artifact the operator sees.
 */
export default defineConfig({
  testDir: ".",
  outputDir: ".pw-report",
  reporter: [["list"]],
  timeout: 90_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: process.env.DRONEOPS_CONSOLE_URL ?? "http://127.0.0.1:4173",
    trace: "off",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium", launchOptions: { args: ["--no-sandbox"] } } }],
});
