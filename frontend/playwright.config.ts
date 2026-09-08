import { defineConfig, devices } from '@playwright/test'

const inheritedEnvironment = Object.fromEntries(
  Object.entries(process.env).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
)
const startLocalApi = process.env.PLAYWRIGHT_START_API === '1'
const externalWebUrl = process.env.FULLSTACK_WEB_URL
const webUrl = externalWebUrl || 'http://127.0.0.1:4173'
const isCI = Boolean(process.env.CI)

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  workers: process.env.FULLSTACK_API_TOKEN ? 1 : undefined,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  timeout: 45_000,
  globalTimeout: isCI ? 10 * 60_000 : 0,
  expect: {
    timeout: 10_000,
  },
  outputDir: 'test-results',
  reporter: isCI ? [['github'], ['html', { open: 'never', outputFolder: 'playwright-report' }]] : 'list',
  use: {
    baseURL: webUrl,
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
    screenshot: { mode: 'only-on-failure', fullPage: true },
    video: { mode: 'retain-on-failure', size: { width: 1280, height: 720 } },
    trace: { mode: 'retain-on-failure', screenshots: true, snapshots: true, sources: true },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: [
    ...(!externalWebUrl ? [{
      name: 'frontend',
      command: 'npm run dev -- --host 127.0.0.1 --port 4173',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    }] : []),
    ...(startLocalApi ? [{
      name: 'api',
      command: 'python -m uvicorn src.fastapi_app:app --host 127.0.0.1 --port 8000',
      cwd: '..',
      url: 'http://127.0.0.1:8000/health',
      reuseExistingServer: true,
      timeout: 60_000,
      env: {
        ...inheritedEnvironment,
        APP_ENV: 'development',
        CADD_API_TOKEN: '',
        CADD_JWT_SECRET: '',
        JOB_BACKEND: 'local',
        AUTO_CREATE_SCHEMA: 'true',
        CORS_ORIGINS: 'http://127.0.0.1:4173',
      },
    }] : []),
  ],
})
