import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { defineConfig } from '@playwright/test'

// The runner and its workers each load this file; the ports go through the environment so
// they all agree. Three free ports, never the team's usual 8000/8009/8010/8020/8021/5173.
if (!process.env.E2E_PORTS) {
  process.env.E2E_PORTS = execFileSync(process.execPath, [
    '-e',
    `const net = require('node:net');
     Promise.all([0, 1, 2].map(() => new Promise((ok) => {
       const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => ok(p)) })
     }))).then((ports) => console.log(ports.join(',')))`,
  ])
    .toString()
    .trim()
}
const [erpPort, apiPort, webPort] = process.env.E2E_PORTS.split(',')

const root = resolve(import.meta.dirname, '../..')
const api = `http://127.0.0.1:${apiPort}`
// `make e2e-integration` recreates this database and loads the frozen pack into it.
const database =
  process.env.E2E_DATABASE_URL ?? 'postgresql+psycopg://trace:trace@localhost:5432/trace_e2e_test'

export default defineConfig({
  testDir: '.',
  outputDir: './test-results/demo',
  testMatch: 'demo-path.spec.ts',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: [
    {
      name: 'erp',
      command: `python3 alberto_erp.py --puerto ${erpPort} --rapido`,
      cwd: resolve(root, '.context/500-sombras-de-alberto'),
      url: `http://127.0.0.1:${erpPort}/erp/estado`,
      reuseExistingServer: false,
    },
    {
      name: 'api',
      command: `uv run uvicorn app.main:app --host 127.0.0.1 --port ${apiPort}`,
      cwd: resolve(root, 'backend'),
      url: `${api}/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        TRACE_DATABASE_URL: database,
        TRACE_ERP_URL: `http://127.0.0.1:${erpPort}`,
        // The demo credentials the challenge publishes (MANUAL_ERP_2009.md, .env.example).
        TRACE_ERP_USER: 'alberto',
        TRACE_ERP_PASSWORD: 'FACTURAS2009',
        TRACEPAY_DATA_DIR: resolve(import.meta.dirname, '.data'),
        // No LLM and no provider OCR: text PDFs need neither, a scan uses the local weights.
        TRACEPAY_OCR_PROFILE: 'experimental',
        TRACEPAY_OCR_MODE: 'local',
        // No key: every model call fails before it is made, as 502 `llm_error`.
        HELMCODE_API_KEY: '',
        OPENAI_API_KEY: '',
        ANTHROPIC_API_KEY: '',
        GEMINI_API_KEY: '',
        GOOGLE_API_KEY: '',
      },
    },
    {
      name: 'console',
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      cwd: resolve(root, 'frontend'),
      url: `http://127.0.0.1:${webPort}`,
      reuseExistingServer: false,
      // The real API, never the mock.
      env: { VITE_API_TARGET: api, VITE_API_MODE: '' },
    },
  ],
})
