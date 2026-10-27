import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

const here = __dirname;
const python =
  process.env.E2E_PYTHON ||
  (process.platform === "win32"
    ? path.resolve(here, "..", ".venv", "Scripts", "python.exe")
    : path.resolve(here, "..", ".venv", "bin", "python"));
const port = Number(process.env.E2E_PORT || 8010);
const baseURL = process.env.E2E_BASE_URL || `http://127.0.0.1:${port}`;
const externalServer = Boolean(process.env.E2E_BASE_URL);

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  timeout: 75_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: [
    ["list"],
    ["html", { outputFolder: "report/html", open: "never" }],
    ["json", { outputFile: "report/results.json" }],
    ["junit", { outputFile: "report/junit.xml" }],
  ],
  outputDir: "report/artifacts",
  use: {
    baseURL,
    locale: "en-PH",
    timezoneId: "Asia/Manila",
    colorScheme: "light",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "firefox-smoke",
      testMatch: /00-smoke\.spec\.ts/,
      use: { ...devices["Desktop Firefox"] },
    },
    {
      name: "webkit-smoke",
      testMatch: /00-smoke\.spec\.ts/,
      use: { ...devices["Desktop Safari"] },
    },
  ],
  ...(externalServer
    ? {}
    : {
        webServer: {
          command: `"${python}" server.py`,
          cwd: here,
          url: `${baseURL}/api/health`,
          reuseExistingServer: false,
          timeout: 180_000,
          stdout: "pipe" as const,
          stderr: "pipe" as const,
        },
      }),
});
