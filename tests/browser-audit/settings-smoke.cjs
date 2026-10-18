const { chromium } = require('../../frontend/node_modules/playwright')
const fs = require('node:fs')
const base = process.env.TRACE_SETTINGS_AUDIT_URL || 'http://127.0.0.1:18174/nexia/trace-it/'
if (!['localhost', '127.0.0.1'].includes(new URL(base).hostname)) throw new Error('Isolated local stack required')
const results = []
const name = `Grant review · investigación Ñ ${Date.now()}`
const out = process.env.TRACE_SETTINGS_AUDIT_REPORT || '/tmp/trace-settings-audit.json'
;(async () => {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await context.newPage()
  page.setDefaultTimeout(12000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  const record = (scenario, evidence) => { results.push({ scenario, evidence }); console.log(scenario, JSON.stringify(evidence)); }
  const visit = async path => { await page.goto(new URL(path, base).href); await page.waitForLoadState('networkidle') }
  try {
    let processId = Number(process.env.TRACE_SETTINGS_PROCESS_ID || 0)
    if (!processId) {
    await visit('processes/new')
    await page.getByRole('button', { name: 'A mano', exact: true }).click()
    record('blank process name blocks submit', await page.getByRole('button', { name: 'Crear proceso', exact: true }).isDisabled())
    await page.getByPlaceholder('Pago de facturas').fill(name)
    await page.getByPlaceholder('Para cada factura, si se paga, no se paga o la ve una persona.').fill('Synthetic grant eligibility review. Approve complete applications; request human review for missing evidence. Amounts are EUR.')
    await page.getByRole('button', { name: 'Crear proceso', exact: true }).click()
    await page.waitForURL(/\/processes\/\d+\/chat$/)
    processId = Number(page.url().match(/processes\/(\d+)/)[1])
    record('manual unicode grant process created', { processId, name })
    fs.writeFileSync('/tmp/trace-settings-process.json', JSON.stringify({ processId, name }))
    }
    await visit(`processes/${processId}/settings?tab=modelos`)
    await page.getByRole('button', { name: 'Save draft and validate', exact: true }).waitFor()
    record('new process execution model settings', (await page.locator('body').innerText()).slice(-1800))
    await page.getByRole('combobox', { name: 'Preview a preset' }).selectOption('balanced')
    await page.getByRole('button', { name: 'Apply to editor' }).click()
    record('preset applied to editor', await page.getByText('Current configuration:', { exact: false }).innerText())
    await visit(`processes/${processId}/definition/fuentes`)
    record('new process sources empty state', await page.getByText('Sin fuentes. Sin ellas, ninguna regla que cruce datos puede decidir.', { exact: true }).isVisible())
    await visit('settings?tab=identity')
    record('identity choices', await page.locator('body').innerText())
    const response = await context.request.get(new URL('api/users', base).href)
    const users = await response.json()
    const operator = users.find(user => user.role === 'operator')
    if (!operator) throw new Error('Fixture operator required')
    await page.getByRole('button').filter({ hasText: operator.email }).click()
    await page.locator('button[aria-pressed="true"]').filter({ hasText: operator.email }).waitFor()
    record('operator selected', await page.getByRole('button').filter({ hasText: operator.email }).getAttribute('aria-pressed'))
    // Client-side navigation preserves the selected identity and exposes role handling.
    await page.getByRole('link', { name: new RegExp('Grant review · investigación Ñ') }).last().click()
    await page.getByRole('link', { name: 'Consola', exact: true }).click()
    await page.getByRole('link', { name: 'Definición', exact: true }).click()
    await page.getByRole('link', { name: 'Fuentes de verdad', exact: true }).click()
    await page.getByRole('button', { name: 'Sincronizar', exact: true }).waitFor()
    const logins = []
    page.on('request', request => { if (request.url().endsWith('/api/login')) logins.push(request.postDataJSON()) })
    await page.getByRole('button', { name: 'Sincronizar', exact: true }).click()
    await page.waitForTimeout(1500)
    record('denied operator sync must not change identity', { logins, text: (await page.locator('body').innerText()).slice(-1800) })
    record('runtime errors', errors)
  } catch(error) {
    process.exitCode = 1
    record('audit exception', String(error))
    await page.screenshot({ path: '/tmp/trace-settings-audit-failure.png', fullPage: true })
  } finally {
    fs.writeFileSync(out, JSON.stringify(results, null, 2))
    console.log(JSON.stringify(results, null, 2))
    await browser.close()
  }
})()
