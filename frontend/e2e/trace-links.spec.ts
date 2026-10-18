import { expect, test, type Page } from '@playwright/test'

const filename = 'factura á + #&?.pdf'

async function fixture(page: Page) {
  const state = { id: 21, missing: false, requested: [] as number[], searches: [] as string[], errors: [] as string[] }
  page.on('pageerror', (error) => state.errors.push(error.message))
  const process = {
    id: 1, name: 'Trace links fixture', description: '', symbols: [],
    decision_types: [
      { name: 'PAGAR', priority: 0, is_default: true, requires_human: false },
      { name: 'ESCALAR', priority: 10, is_default: false, requires_human: true },
    ],
  }
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/src/')) return route.continue()
    const path = url.pathname.split('/api')[1]
    let body: unknown = []
    if (path === '/login') body = { id: 1, name: 'Reviewer', email: 'test@ci.invalid', role: 'manager' }
    if (path === '/processes') body = [process]
    if (path === '/processes/1') body = process
    if (path === '/processes/1/summary') body = { queue: 0, by_status: {}, by_decision: {}, instances: 3 }
    if (path?.includes('/mail-ingestion/activity')) body = { initialized: true, latest_id: 0, items: [], has_more: false }
    if (path === '/processes/1/instances') {
      state.searches.push(url.searchParams.get('q') ?? '')
      // A substring match with a larger ID must never override the requested document.
      body = [
        { id: 99999, name: `${filename}.backup`, status: 'DECIDED', decision: 'PAGAR' },
        ...(!state.missing ? [
          { id: state.id - 1, name: filename, status: 'DECIDED', decision: 'PAGAR' },
          { id: state.id, name: filename, status: 'DECIDED', decision: 'PAGAR' },
        ] : []),
      ]
    }
    const instance = path?.match(/^\/instances\/(\d+)$/)
    if (path?.match(/^\/instances\/\d+\/trace$/)) body = {
      decisions: [], sources_read: [], spans: [],
      pending: { waiting_for_person: false, review_pending: false, proposals: [], alerts: [] },
    }
    if (instance) {
      const id = Number(instance[1])
      state.requested.push(id)
      body = { id, process_id: 1, name: filename, status: 'DECIDED', decision: 'PAGAR',
        decisions: [], reviews: [], events: [], symbols: {}, values: {}, review_pending: false }
    }
    await route.fulfill({ json: body })
  })
  return state
}

test('exported document link opens the latest exact case after its ID changes', async ({ page }) => {
  const state = await fixture(page)
  const link = `processes/1/review?${new URLSearchParams({ file: filename })}`
  await page.goto(link)
  await expect(page.getByRole('link', { name: 'Ver traza →' })).toHaveAttribute('href', /instances\?i=21$/)
  expect(state.searches).toContain(filename)
  expect(state.requested).toEqual([21])
  state.id = 501 // A reset creates a new instance for the same document.
  await page.reload()
  await expect(page.getByRole('link', { name: 'Ver traza →' })).toHaveAttribute('href', /instances\?i=501$/)
  expect(state.requested).toEqual([21, 501])
  await expect(page).toHaveURL(new RegExp('file='))
  expect(state.errors).toEqual([])
})

test('missing document shows a notice instead of another case', async ({ page }) => {
  const state = await fixture(page)
  state.missing = true
  await page.goto(`processes/1/review?${new URLSearchParams({ file: filename })}`)
  await expect(page.getByText('Documento no encontrado', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Ver traza →' })).toHaveCount(0)
  expect(state.requested).toEqual([])
  expect(state.errors).toEqual([])
})

test('existing numeric case links remain supported', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('processes/1/review?i=21')
  await expect(page.getByRole('link', { name: 'Ver traza →' })).toHaveAttribute('href', /instances\?i=21$/)
  expect(state.searches).toEqual([])
  expect(state.errors).toEqual([])
})

test('execution steps have a nested layout and distinct operation icons', async ({ page }) => {
  const state = await fixture(page)
  await page.route('**/api/instances/21/trace', route => route.fulfill({ json: {
    id: 21, process_id: 1, decisions: [], sources_read: [],
    pending: { waiting_for_person: false, review_pending: false, proposals: [], alerts: [] },
    spans: [{ span_id: 'run', step: 'run_process', status: 'ok', duration_ms: 100,
      data: {}, children: [{ span_id: 'rule', step: 'evaluate_rule', status: 'ok',
        duration_ms: 10, data: { rule_id: 1 }, children: [] }] }],
  } }))
  await page.goto('processes/1/instances?i=21')
  await page.getByRole('button', { name: /Execution trace|Traza de ejecución/ }).click()
  const parent = page.locator('summary').filter({ hasText: 'Run process' })
  await expect(parent).toBeVisible()
  await parent.click()
  const child = page.locator('summary').filter({ hasText: 'Evaluate rule' })
  await expect(child).toBeVisible()
  expect((await child.boundingBox())!.x).toBeGreaterThan((await parent.boundingBox())!.x)
  await expect(parent.locator('svg').first()).toHaveClass(/lucide-play/)
  await expect(child.locator('svg').first()).toHaveClass(/lucide-list-checks/)
  await child.click()
  await expect(page.getByText('rule_id', { exact: false })).toBeVisible()
  expect(state.errors).toEqual([])
})

test('published versions can run backtests and show the impact', async ({ page }) => {
  const state = await fixture(page)
  await page.route('**/api/processes/1/versions', route => route.fulfill({ json: [{
    id: 7, process_id: 1, number: 2, parent_id: null, author: 'Manager', reason: '',
    created_at: '2026-09-20T00:00:00Z', snapshot: { process: {}, rules: [] },
    validation: { unchanged: 0 },
  }] }))
  let calls = 0
  await page.route('**/api/process-versions/7/backtest', route => {
    calls++
    return route.fulfill({ json: { valid: true, hash: 'test', unchanged: 1,
      coverage: { total: 2, evaluated: 2, not_evaluable: 0, partial: 0, none: 0 },
      changes: [{ instance_id: 21, name: 'changed.pdf', before: 'PAY', after: 'CHECK' }],
    } })
  })
  await page.goto('processes/1/definition/normas')
  await page.getByRole('button', { name: 'v2', exact: true }).click()
  await page.getByRole('menuitemradio').click()
  await page.getByRole('button', { name: 'Run backtest', exact: true }).click()
  await expect(page.getByText('PAY → CHECK · 1')).toBeVisible()
  expect(calls).toBe(1)
  expect(state.errors).toEqual([])
})

test('empty execution search clears the previous document detail', async ({ page }) => {
  const state = await fixture(page)
  await page.route('**/api/processes/1/instances?*', route => route.fulfill({ json: [] }))
  await page.goto('processes/1/instances?i=21')
  await expect(page.getByText('Decisión final', { exact: true })).toBeVisible()
  await page.getByPlaceholder('Nombre del archivo…').fill('no such document')
  await expect(page.getByText('Elige un documento', { exact: true })).toBeVisible()
  await expect(page.getByText('Decisión final', { exact: true })).toHaveCount(0)
  expect(state.errors).toEqual([])
})

test('missing instance links display their error without substituting another document', async ({ page }) => {
  const state = await fixture(page)
  await page.route('**/api/instances/777', route => route.fulfill({
    status: 404, json: { code: 'not_found', message: 'Requested document does not exist' },
  }))
  await page.goto('processes/1/instances?i=777')
  await expect(page.getByText('Requested document does not exist', { exact: true })).toBeVisible()
  await expect(page).toHaveURL(/i=777$/)
  await expect(page.getByText('Decisión final', { exact: true })).toHaveCount(0)
  expect(state.requested).toEqual([])
  expect(state.errors).toEqual([])
})

test('failed upload waits for in-flight requests and stops sending the rest of the batch', async ({ page }) => {
  const state = await fixture(page)
  await page.route('**/api/processes/1/rules', route => route.fulfill({ json: [{ id: 1, status: 'active' }] }))
  let uploadCount = 0
  let firstFailure = () => {}
  let finishRemaining = () => {}
  const failed = new Promise<void>(resolve => { firstFailure = resolve })
  const remaining = new Promise<void>(resolve => { finishRemaining = resolve })
  await page.route('**/api/processes/1/files', async route => {
    const index = uploadCount++
    if (index === 0) {
      await failed
      await route.fulfill({ status: 422, json: { code: 'invalid_file', message: 'Test upload rejected' } })
    } else {
      await remaining
      await route.fulfill({ json: { instance_id: index, name: `test-${index}.pdf`, status: 'DECIDED', created: true, symbols: {} } })
    }
  })
  await page.goto('processes/1')
  await expect(page.getByRole('button', { name: 'Subir documentos', exact: true })).toBeEnabled()
  await page.locator('input[type="file"]').first().setInputFiles(
    Array.from({ length: 8 }, (_, index) => ({ name: `test-${index}.pdf`, mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 test') })),
  )
  await expect.poll(() => uploadCount).toBe(4)
  const failureResponse = page.waitForResponse(response => response.url().endsWith('/files') && response.status() === 422)
  firstFailure()
  await failureResponse
  // Let the client consume the failure before settling the other workers.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  await expect(page.getByRole('button', { name: 'Procesando…', exact: true })).toBeDisabled()
  finishRemaining()
  await expect(page.getByText('Test upload rejected', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Subir documentos', exact: true })).toBeEnabled()
  expect(uploadCount).toBe(4)
  expect(state.errors).toEqual([])
})

test('default outcome with an empty reason is still presented as decided', async ({ page }) => {
  const state = await fixture(page)
  await page.route('**/api/instances/21', route => route.fulfill({ json: {
    id: 21, process_id: 1, name: filename, status: 'DECIDED', decision: 'PAGAR',
    decisions: [{ decision: 'PAGAR', reason: '', author: 'engine', rules_hash: 'fixture', created_at: '2026-10-18T00:00:00Z' }],
    reviews: [], events: [], symbols: {}, values: {}, review_pending: false,
  } }))
  await page.goto('processes/1/instances?i=21')
  await expect(page.getByText('Todas las comprobaciones del proceso han pasado', { exact: true })).toBeVisible()
  await expect(page.getByText('El proceso todavía no ha emitido una decisión.', { exact: true })).toHaveCount(0)
  expect(state.errors).toEqual([])
})

test('failed sources request is shown as an error and can be retried', async ({ page }) => {
  const state = await fixture(page)
  let failing = true
  await page.route('**/api/processes/1/sources', route => route.fulfill(failing
    ? { status: 503, json: { code: 'sources_unavailable', message: 'Test sources unavailable' } }
    : { json: [] },
  ))
  await page.goto('processes/1/definition/fuentes')
  await expect(page.getByText('Test sources unavailable', { exact: true })).toBeVisible()
  await expect(page.getByText('Sin fuentes. Sin ellas, ninguna regla que cruce datos puede decidir.', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Sincronizar', exact: true })).toHaveCount(0)
  failing = false
  await page.getByRole('button', { name: 'Reintentar', exact: true }).click()
  await expect(page.getByText('Sin fuentes. Sin ellas, ninguna regla que cruce datos puede decidir.', { exact: true })).toBeVisible()
  expect(state.errors).toEqual([])
})
