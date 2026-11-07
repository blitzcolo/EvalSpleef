// web-001 Playwright 配置。APP_DIR 指向被测工作区（默认题包 workspace/）。
const path = require("path");
const { defineConfig } = require("@playwright/test");

const appDir = process.env.APP_DIR || path.join(__dirname, "..", "workspace");

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: ["kanban.spec.js"],
  timeout: 30000,
  expect: { timeout: 5000 },
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "python3 -m http.server 4173 --directory " + appDir,
    port: 4173,
    reuseExistingServer: false,
    timeout: 20000,
  },
});
