import { defineConfig, devices } from '@playwright/test';
import { config as loadEnv } from 'dotenv';
import fs from 'node:fs';
import path from 'node:path';

type EnvConfig = {
  path: string;
  override?: boolean;
};

const workspaceRoot = path.resolve(__dirname, '..');
const repoRoot = path.resolve(workspaceRoot, '..');

const envFiles: EnvConfig[] = [
  { path: path.join(repoRoot, '.env') },
  { path: path.join(repoRoot, '.env.local'), override: true },
  { path: path.join(workspaceRoot, '.env') },
  { path: path.join(workspaceRoot, '.env.local'), override: true },
  { path: path.join(__dirname, '.env') },
  { path: path.join(__dirname, '.env.local'), override: true },
];

for (const entry of envFiles) {
  if (fs.existsSync(entry.path)) {
    loadEnv({ path: entry.path, override: entry.override ?? false });
  }
}

const baseURL = process.env.UI_BASE_URL ?? 'http://localhost:3000';
const headless = !/^true$/i.test(process.env.PLAYWRIGHT_HEADED ?? '');

export default defineConfig({
  testDir: './playwright',
  fullyParallel: false,
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: path.join(workspaceRoot, 'artifacts/report'), open: 'never' }],
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
  outputDir: path.join(workspaceRoot, 'artifacts/test-results'),
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
      },
    },
  ],
});
