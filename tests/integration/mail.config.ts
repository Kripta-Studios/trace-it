import { resolve } from 'node:path'
import { defineConfig } from '@playwright/test'

const root = resolve(import.meta.dirname, '../..')
const api = process.env.MAIL_TEST_API_URL ?? 'http://127.0.0.1:18285'
const webPort = process.env.MAIL_TEST_WEB_PORT ?? '15285'
export default defineConfig({
  outputDir: './test-results/mail',
  testDir: '.', testMatch: 'mail.spec.ts', timeout: 60_000, workers: 1, retries: 0,
  expect: { timeout: 30_000 }, reporter: 'list',
  use: { baseURL: `http://127.0.0.1:${webPort}`, trace: 'retain-on-failure' },
  webServer: [
    ...process.env.MAIL_TEST_EXTERNAL_API ? [] : [{
      command: 'uv run uvicorn tests.support.mail_app:app --host 127.0.0.1 --port 18285',
      cwd: resolve(root, 'backend'), url: `${api}/health`, reuseExistingServer: false,
      env: {
        TRACE_DATABASE_URL: process.env.MAIL_TEST_DATABASE_URL ?? 'postgresql+psycopg://trace:trace@localhost:5432/trace_mail_browser_test',
        MAIL_TEST_API_URL: api, TRACEPAY_OCR_PROFILE: 'experimental',
        HELMCODE_API_KEY: '', OPENAI_API_KEY: '', ANTHROPIC_API_KEY: '',
        GEMINI_API_KEY: '', GOOGLE_API_KEY: '', JEV_API_KEY: '',
      },
    }],
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      cwd: resolve(root, 'frontend'), url: `http://127.0.0.1:${webPort}`,
      reuseExistingServer: false, env: {
        VITE_API_TARGET: api, VITE_API_MODE: '',
        VITE_DEFAULT_USER_EMAIL: 'mail-browser@example.invalid',
      },
    },
  ],
})
