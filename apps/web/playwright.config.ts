import { defineConfig, devices } from "@playwright/test";
import { existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(webRoot, "../..");
const runtimeRoot = path.join(tmpdir(), "a-share-swing-quant-e2e");
const databasePath = path.join(runtimeRoot, "quant-system.db");
const researchPath = path.join(runtimeRoot, "research");
mkdirSync(runtimeRoot, { recursive: true });

const pythonCandidates = process.platform === "win32"
  ? [path.join(repoRoot, ".venv", "Scripts", "python.exe")]
  : [path.join(repoRoot, ".venv", "bin", "python")];
const python = process.env.E2E_PYTHON ?? pythonCandidates.find(existsSync) ?? "python";
const quote = (value: string) => `"${value.replaceAll('"', '\\"')}"`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["line"], ["html", { open: "never", outputFolder: path.join(runtimeRoot, "playwright-report") }]]
    : "line",
  outputDir: path.join(runtimeRoot, "test-results"),
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: `${quote(python)} -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000`,
      cwd: repoRoot,
      url: "http://127.0.0.1:8000/api/v1/health/live",
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        QUANT_DATA_PROVIDER: "demo",
        QUANT_DB_PATH: databasePath,
        QUANT_RESEARCH_PATH: researchPath,
        QUANT_ADMIN_API_KEY: "",
      },
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
      cwd: webRoot,
      url: "http://127.0.0.1:4173",
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
