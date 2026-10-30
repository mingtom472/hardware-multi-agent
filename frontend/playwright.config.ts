import { defineConfig } from "@playwright/test";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const browserPath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3100",
    browserName: "chromium",
    launchOptions: browserPath ? { executablePath: browserPath } : {},
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: ".venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8100",
      cwd: root,
      env: { HW_MODEL_MODE: "mock", HW_DATA_DIR: ".local/e2e-data" },
      url: "http://127.0.0.1:8100/health",
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --port 3100",
      env: { BACKEND_URL: "http://127.0.0.1:8100", NEXT_TELEMETRY_DISABLED: "1" },
      url: "http://127.0.0.1:3100",
      timeout: 90_000,
    },
  ],
});
