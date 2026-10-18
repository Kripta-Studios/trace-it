import { expect, test, type Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'

async function settingsFixture(page: Page) {
  const manager = { id: 1, name: 'Manager', email: 'martin@trace-it.local', role: 'manager' }
  const operator = { id: 2, name: 'Reader', email: 'reader@audit.invalid', role: 'operator' }
  const process = { id: 1, name: 'Grant review', description: '', symbols: [], decision_types: [] }
  const logins: string[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/**', async route => {
    const request = route.request()
    if (request.url().includes('/src/')) return route.continue()
    const path = new URL(request.url()).pathname.split('/api')[1]
    let body: unknown = []
    if (path === '/login') {
      const email = request.postDataJSON().email
      logins.push(email)
      body = email === operator.email ? operator : manager
    }
    if (path === '/users') body = [manager, operator]
    if (path === '/processes') body = [process]
    if (path === '/processes/1') body = process
    if (path === '/processes/1/summary') body = { queue: 0, instances: 0, by_status: {}, by_decision: {} }
    if (path === '/processes/1/sources/erp/sync') return route.fulfill({ status: 403, json: { code: 'permission_denied', message: 'Only a manager can do this' } })
    if (path?.includes('/mail-ingestion/activity')) body = { initialized: true, latest_id: 0, items: [], has_more: false }
    if (path === '/health') body = { status: 'ok' }
    return route.fulfill({ json: body })
  })
  return { logins, errors }
}

test('permission denial preserves the explicitly selected operator identity', async ({ page }) => {
  const state = await settingsFixture(page)
  await page.goto('settings?tab=identity')
  await page.getByRole('button').filter({ hasText: 'reader@audit.invalid' }).click()
  await expect(page.getByRole('button').filter({ hasText: 'reader@audit.invalid' })).toHaveAttribute('aria-pressed', 'true')
  await page.reload()
  await expect(page.getByRole('button').filter({ hasText: 'reader@audit.invalid' })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('link', { name: 'Grant review', exact: true }).click()
  await page.getByRole('link', { name: 'Consola', exact: true }).click()
  await page.getByRole('link', { name: 'Definición', exact: true }).click()
  await page.getByRole('link', { name: 'Fuentes de verdad', exact: true }).click()
  await page.getByRole('button', { name: 'Sincronizar', exact: true }).click()
  await expect(page.getByText('Only a manager can do this', { exact: true })).toBeVisible()
  await page.locator('a[href="/nexia/trace-it/settings"]').first().click()
  await page.getByRole('button', { name: /^(Identity|Identidad)$/ }).click()
  await expect(page.getByRole('button').filter({ hasText: 'reader@audit.invalid' })).toHaveAttribute('aria-pressed', 'true')
  expect(state.logins).toEqual(['martin@trace-it.local', 'reader@audit.invalid', 'reader@audit.invalid'])
  expect(state.errors).toEqual([])
})

test('export server errors remain visible and can be dismissed', async ({ page }) => {
  const state = await settingsFixture(page)
  await page.route('**/api/processes/1/export*', route => route.fulfill({ status: 503, json: { code: 'export_failed', message: 'Synthetic export outage' } }))
  await page.goto('processes/1/instances')
  await page.getByRole('button', { name: 'Exportar', exact: true }).click()
  await expect(page.getByText('Synthetic export outage', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Entendido', exact: true }).click()
  await expect(page.getByText('Synthetic export outage', { exact: true })).toHaveCount(0)
  expect(state.errors).toEqual([])
})

test('expired operator identity is refreshed as the same operator', async ({ page }) => {
  const state = await settingsFixture(page)
  await page.addInitScript(() => sessionStorage.setItem('trace.identity', 'reader@audit.invalid'))
  await page.route('**/api/use-cases', route => route.fulfill({ status: 401, json: { code: 'unauthenticated', message: 'Identity needs refreshing' } }))
  await page.goto('settings?tab=models')
  await expect(page.getByText('Identity needs refreshing', { exact: true })).toBeVisible()
  await expect.poll(() => state.logins.length).toBe(2)
  expect(state.logins).toEqual(['reader@audit.invalid', 'reader@audit.invalid'])
  expect(state.errors).toEqual([])
})

test('deleted stored account requires explicit recovery instead of silently selecting a manager', async ({ page }) => {
  const state = await settingsFixture(page)
  await page.addInitScript(() => sessionStorage.setItem('trace.identity', 'deleted@audit.invalid'))
  const attempted: string[] = []
  await page.route('**/api/login', route => {
    const email = route.request().postDataJSON().email
    attempted.push(email)
    return email === 'deleted@audit.invalid'
      ? route.fulfill({ status: 404, json: { code: 'not_found', message: 'Stored user no longer exists' } })
      : route.fallback()
  })
  await page.goto('settings?tab=identity')
  await expect(page.getByText('Stored user no longer exists', { exact: true })).toBeVisible()
  expect(attempted).toEqual(['deleted@audit.invalid'])
  await page.getByRole('button', { name: 'Usar identidad predeterminada', exact: true }).click()
  await expect(page.getByRole('button').filter({ hasText: 'martin@trace-it.local' })).toHaveAttribute('aria-pressed', 'true')
  expect(attempted).toEqual(['deleted@audit.invalid', 'martin@trace-it.local'])
  expect(state.errors).toEqual([])
})

test('identity remains usable when browser session storage is unavailable', async ({ page }) => {
  const state = await settingsFixture(page)
  await page.addInitScript(() => Object.defineProperty(window, 'sessionStorage', { get() { throw new DOMException('Storage disabled', 'SecurityError') } }))
  await page.goto('settings?tab=identity')
  await expect(page.getByRole('button').filter({ hasText: 'martin@trace-it.local' })).toHaveAttribute('aria-pressed', 'true')
  expect(state.errors).toEqual([])
})

test('model overrides can be cleared and operators see read-only model fields', async ({ page }) => {
  await settingsFixture(page)
  const agent = { id: 9, role: 'compiler', version: 1, config: { model: 'provider:custom' } }
  await page.route('**/api/use-cases', route => route.fulfill({ json: [{ id: 1, name: 'Grants' }] }))
  await page.route('**/api/use-cases/1', route => route.fulfill({ json: { id: 1, name: 'Grants', agents: [agent] } }))
  const updates: unknown[] = []
  await page.route('**/api/use-cases/1/agents/compiler', route => {
    const data = route.request().postDataJSON()
    updates.push(data.config.model)
    agent.config = data.config
    agent.version++
    return route.fulfill({ json: agent })
  })
  await page.goto('settings?tab=models')
  const model = page.getByRole('textbox', { name: 'compiler model' })
  await model.fill('')
  await model.blur()
  await expect.poll(() => updates).toEqual([null])
  await page.reload()
  await expect(model).toHaveValue('')
  await page.getByRole('button', { name: /^(Identity|Identidad)$/ }).click()
  await page.getByRole('button').filter({ hasText: 'reader@audit.invalid' }).click()
  await expect(page.getByRole('button').filter({ hasText: 'reader@audit.invalid' })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: /^(Models|Modelos)$/ }).click()
  await expect(model).toHaveJSProperty('readOnly', true)
})

test('workbook warnings expose incomplete fields and ignored sheets', async ({ page }) => {
  await settingsFixture(page)
  await page.route('**/api/processes/1/sources/workbook', route => route.fulfill({ status: 201, json: {
    process_id: 1, sources: [], warnings: [
      { code: 'FIELD_MISSING', field: 'supplier_tax_id', locator: 'Orders!42' },
      { code: 'UNRECOGNIZED_SCHEMA', sheet: 'Archive 2024' },
    ],
  } }))
  await page.goto('processes/1/definition/fuentes')
  await page.getByLabel('Fecha de corte').fill('2026-10-18')
  await page.locator('input[type=file]').setInputFiles({ name: 'sources.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('fixture') })
  await expect(page.getByText('2 avisos al cargar el Excel', { exact: true })).toBeVisible()
  await page.getByText('Ver detalles', { exact: true }).click()
  await expect(page.getByText('Falta un campo · Orders!42 · supplier_tax_id', { exact: true })).toBeVisible()
  await expect(page.getByText('Hoja no reconocida · Archive 2024', { exact: true })).toBeVisible()
})

test('custom process cases without invoice totals show an unavailable amount instead of zero', async ({ page }) => {
  await settingsFixture(page)
  await page.route('**/api/processes/1/queue', route => route.fulfill({ json: [{
    id: 12, process_id: 1, name: 'travel-claim.pdf', status: 'DECIDED', decision: 'REVISAR',
    values: { amount: 180, employee: 'Ana' }, reason: 'Needs review', review_pending: false,
  }] }))
  await page.goto('processes/1')
  const total = page.getByText('Importe conocido', { exact: true }).locator('..')
  await expect(total).toContainText('—')
  await expect(total).not.toContainText('0,00')
})

for (const screen of ['inbox', 'panel']) {
  test(`${screen} batch runs only newly uploaded instances, leaving older pending documents alone`, async ({ page }) => {
    await settingsFixture(page)
    await page.route('**/api/processes/1/rules', route => route.fulfill({ json: [{ id: 1, status: 'active' }] }))
    await page.route('**/api/processes/1/execution', route => route.fulfill({ json: { revision: null, version_id: 1 } }))
    const pending = new Set([6, 7])
    await page.route('**/api/processes/1/summary', route => route.fulfill({ json: {
      queue: 0, instances: 2, by_status: { PENDING: pending.size }, by_decision: {},
    } }))
    await page.route('**/api/processes/1/files', route => route.fulfill({ json: {
      instance_id: 7, name: 'new-claim.pdf', status: 'PENDING', created: true, symbols: { amount: 180 },
    } }))
    const requests: unknown[] = []
    await page.route('**/api/processes/1/run', route => {
      const body = route.request().postDataJSON()
      requests.push(body)
      const selected: number[] = body?.instance_ids ?? [...pending]
      selected.forEach(id => pending.delete(id))
      return route.fulfill({ json: { run_id: 1, decided: selected.length, by_decision: { REJECT: selected.length }, elapsed_ms: 1, version_number: 1 } })
    })
    await page.goto(screen === 'inbox' ? 'processes/1' : 'processes/1/panel')
    if (screen === 'panel') await page.getByRole('button', { name: 'Ejecutar', exact: true }).click()
    await page.locator('input[type=file]').first().setInputFiles({ name: 'new-claim.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 fixture') })
    if (screen === 'panel') await page.getByRole('button', { name: 'Ejecutar 1', exact: true }).click()
    await expect.poll(() => requests).toEqual([{ instance_ids: [7] }])
    expect([...pending]).toEqual([6])
  })
}

test('console exports the latest human resolution with trace data', async ({ page }) => {
  const state = await settingsFixture(page)
  const requests: URL[] = []
  await page.route('**/api/processes/1/export*', route => {
    const url = new URL(route.request().url())
    requests.push(url)
    const full = url.searchParams.get('full') === 'true'
    return route.fulfill({
      contentType: 'application/x-ndjson',
      body: JSON.stringify({
        file_id: 'reviewed-travel.pdf',
        result: full ? 'APPROVE' : 'REVIEW',
        ...(full ? { decided_by: 'Manager', reason: 'Receipt checked', trace_url: '/processes/1/review?i=12' } : {}),
      }) + '\n',
    })
  })
  await page.goto('processes/1/instances')
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Exportar', exact: true }).click()
  const download = await downloaded
  expect(download.suggestedFilename()).toBe('outcomes.jsonl')
  const contents = JSON.parse((await readFile((await download.path())!, 'utf8')).trim())
  expect(contents).toMatchObject({ result: 'APPROVE', decided_by: 'Manager', reason: 'Receipt checked' })
  expect(contents.trace_url).toContain('/review?i=12')
  expect(requests).toHaveLength(1)
  expect(requests[0].searchParams.get('full')).toBe('true')
  expect(state.errors).toEqual([])
})
