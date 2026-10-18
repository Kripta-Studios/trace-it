import { expect, test, type Page } from '@playwright/test'

async function fixture(page: Page) {
  const calls: URL[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const document = (id: number, name: string, excerpt: string) => ({
    instance_id: id, name, excerpt, status: 'DECIDED', uploaded_at: '2026-10-18T12:00:00Z', has_text: true, has_evidence: false,
  })
  const documents = [document(21, 'contract-alpha.txt', 'Warranty reference ALPHA-123'), document(20, 'invoice-beta.txt', 'Payment reference BETA-456')]
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/src/')) return route.continue()
    const path = url.pathname.split('/api')[1]
    let body: unknown = []
    if (path === '/auth-config') body = { mode: 'identity', session_hours: 12 }
    if (path === '/login') body = { id: 1, name: 'Reviewer', email: 'test@ci.invalid', role: 'manager' }
    if (path === '/processes') body = [{ id: 1, name: 'Knowledge fixture' }]
    if (path === '/processes/1') body = { id: 1, name: 'Knowledge fixture', symbols: [], decision_types: [] }
    if (path === '/processes/1/summary') body = { queue: 0, instances: 2, by_status: {}, by_decision: {} }
    if (path?.includes('/mail-ingestion/activity')) body = { initialized: true, latest_id: 0, items: [], has_more: false }
    if (path === '/processes/1/knowledge') {
      calls.push(url)
      const query = url.searchParams.get('query') ?? ''
      const cursor = url.searchParams.get('cursor')
      body = query ? { items: documents.filter(item => `${item.name} ${item.excerpt}`.toLowerCase().includes(query.toLowerCase())), next_cursor: null, query }
        : { items: cursor ? [documents[1]] : [documents[0]], next_cursor: cursor ? null : 21, query }
    }
    if (path === '/instances/21/file') return route.fulfill({ contentType: 'text/plain', body: 'Warranty reference ALPHA-123' })
    return route.fulfill({ json: body })
  })
  return { calls, errors }
}

test('library searches, paginates, downloads originals and restores URL search after Back', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('processes/1/documentation')
  await expect(page.getByRole('heading', { name: 'Documentación del proceso' })).toBeVisible()
  await page.getByRole('button', { name: 'Mostrar más documentos' }).click()
  await expect(page.getByText('invoice-beta.txt', { exact: true })).toBeVisible()
  expect(state.calls.at(-1)?.searchParams.get('cursor')).toBe('21')
  const search = page.getByRole('textbox', { name: 'Buscar documentación' })
  await search.fill('ALPHA-123')
  await page.getByRole('button', { name: 'Buscar', exact: true }).click()
  await expect(page.getByText('invoice-beta.txt', { exact: true })).toHaveCount(0)
  await page.getByRole('button').filter({ hasText: 'contract-alpha.txt' }).click()
  const preview = page.getByRole('region', { name: 'Evidencia de contract-alpha.txt' })
  await expect(preview).toBeVisible()
  await expect(preview.getByRole('link', { name: 'Abrir caso' })).toHaveAttribute('href', /\/instances\?i=21$/)
  const downloaded = page.waitForEvent('download')
  await preview.getByRole('button', { name: 'Descargar', exact: true }).click()
  expect((await downloaded).suggestedFilename()).toBe('contract-alpha.txt')
  await search.fill('no-matches')
  await page.getByRole('button', { name: 'Buscar', exact: true }).click()
  await expect(page.getByText('No se encontraron documentos', { exact: true })).toBeVisible()
  await expect(preview).toHaveCount(0)
  await page.goBack()
  await expect(search).toHaveValue('ALPHA-123')
  await expect(page.getByText('contract-alpha.txt', { exact: true })).toBeVisible()
  await expect(preview).toHaveCount(0)
  await page.getByRole('button', { name: 'Limpiar', exact: true }).click()
  await expect(search).toHaveValue('')
  await expect(page.getByText('invoice-beta.txt', { exact: true })).toBeVisible()
  expect(state.errors).toEqual([])
})

test('library handles request failures and mobile search plus evidence fit the viewport', async ({ page }) => {
  const state = await fixture(page)
  await page.setViewportSize({ width: 390, height: 844 })
  let failing = true
  await page.route('**/api/processes/1/knowledge?*', route => failing
    ? route.fulfill({ status: 503, json: { code: 'unavailable', message: 'Synthetic library outage' } })
    : route.fallback())
  await page.goto('processes/1/documentation?q=ALPHA-123')
  await expect(page.getByText('Synthetic library outage', { exact: true })).toBeVisible()
  await expect(page.getByText('No se encontraron documentos', { exact: true })).toHaveCount(0)
  failing = false
  await page.getByRole('button', { name: 'Reintentar', exact: true }).click()
  await page.getByRole('button').filter({ hasText: 'contract-alpha.txt' }).click()
  await expect(page.getByRole('region', { name: 'Evidencia de contract-alpha.txt' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  for (const name of ['Buscar', 'Limpiar']) {
    const rect = await page.getByRole('button', { name, exact: true }).boundingBox()
    expect(rect!.x).toBeGreaterThanOrEqual(0)
    expect(rect!.x + rect!.width).toBeLessThanOrEqual(390)
  }
  await page.getByRole('button', { name: 'Cerrar documento', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Evidencia de contract-alpha.txt' })).toHaveCount(0)
  expect(state.errors).toEqual([])
})
