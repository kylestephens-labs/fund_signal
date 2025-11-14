import { defineConfig, devices } from '@playwright/test';
import { config as loadEnv } from 'dotenv';
import fs from 'node:fs';
import path from 'node:path';

type EnvConfig = {
  path: string;
  override?: boolean;
};

const envFiles: EnvConfig[] = [
  { path: path.resolve(__dirname, '../.env') },
  { path: path.resolve(__dirname, '../.env.local'), override: true },
  { path: path.resolve(__dirname, '.env') },
  { path: path.resolve(__dirname, '.env.local'), override: true },
];

for (const entry of envFiles) {
  if (fs.existsSync(entry.path)) {
    loadEnv({ path: entry.path, override: entry.override ?? false });
  }
}

const baseURL = process.env.UI_BASE_URL ?? 'http://localhost:3000';
const headless = !/^true$/i.test(process.env.PLAYWRIGHT_HEADED ?? '');

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'artifacts/report', open: 'never' }],
  ],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    headless,
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  outputDir: 'artifacts/test-results',
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
      },
    },
  ],
});
