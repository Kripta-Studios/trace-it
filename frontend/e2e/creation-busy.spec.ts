import { expect, test, type Page } from '@playwright/test'

async function preparedDraft(page: Page, history?: 'incomplete' | 'absent') {
  const session = {
    id: 901, process_id: history ? 8 : null, published_process_id: null, revision: 4,
    name: 'Library acquisition', messages: [], reviews: { setup: 'accepted' }, snapshots: [],
    changes: [{ field: 'name', before: '', after: 'Library acquisition' }],
    plan: {
      name: 'Library acquisition', description: 'Synthetic test process',
      symbols: [
        { name: 'estimated_cost', type: 'number', required: true, extraction: { source: 'none', labels: [] } },
        { name: 'copies', type: 'number', required: true, extraction: { source: 'document', labels: ['copies', 'Copies requested'] } },
        { name: 'request_note', type: 'text', required: false, extraction: { source: 'none', labels: [] } },
        { name: 'optional_reference', type: 'text', extraction: { source: 'none', labels: [] } },
      ], decision_types: [{ name: 'APPROVE', priority: 1, is_default: true, requires_human: false }],
      connectors: [], sources: [], rules: [], guidance: [], examples: [], questions: [],
    },
    preview: {
      valid: true, examples: [], compilations: [],
      examples_basis: 'Acceptance examples test supplied values, not document ingestion or extraction.',
      extraction_warnings: [{ code: 'REQUIRED_INPUT_EXTRACTION_DISABLED', symbol: 'estimated_cost', source: 'none', message: 'Required input is not read from documents.' }],
      impact: history ? {
        unchanged: 3, changes: [], conflicts: [],
        ...(history === 'incomplete' ? {
          coverage: { total: 3, evaluated: 0, not_evaluable: 3, partial: 0, none: 3 },
          not_evaluable: [{ instance_id: 3, name: 'library-boundary.html', missing_symbols: ['estimated_cost', 'copies'] }],
        } : {}),
      } : undefined,
    },
  }
  let release = () => {}
  const pending = new Promise<void>(resolve => { release = resolve })
  const writes: string[] = []
  await page.route('**/api/**', async route => {
    const request = route.request()
    if (new URL(request.url()).pathname.includes('/src/')) return route.continue()
    const path = new URL(request.url()).pathname.split('/api')[1]
    let body: unknown = []
    if (path === '/login') body = { id: 1, name: 'Manager', email: 'manager@example.invalid', role: 'manager' }
    if (path === '/process-drafts') body = [session]
    if (path === '/process-drafts/901') body = session
    if (path === '/processes') body = history ? [{ id: 8, ...session.plan }] : []
    if (path === '/processes/8') body = { id: 8, ...session.plan }
    if (path === '/processes/8/execution') body = { revision: null, version_id: 1 }
    if (path === '/processes/8/summary') body = { queue: 0 }
    if (request.method() === 'POST' && path !== '/login') {
      writes.push(path)
      await pending
      return route.fulfill({ status: 503, json: { code: 'synthetic_failure', message: 'Retry the operation' } })
    }
    await route.fulfill({ json: body })
  })
  await page.goto(history ? 'processes/8/chat?draft=901' : 'processes/new?draft=901')
  await expect(page.getByRole('button', { name: 'Publicar', exact: true })).toBeEnabled()
  return { release, writes }
}

test('rechecking a prepared draft prevents duplicate checks and publication until it completes', async ({ page }) => {
  const { release, writes } = await preparedDraft(page)
  try {
    await page.getByRole('button', { name: 'Repetir comprobaciones' }).click()
    await expect.poll(() => writes.length).toBe(1)
    await expect(page.getByRole('button', { name: 'Repetir comprobaciones', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Publicar', exact: true })).toBeDisabled()
    await expect(page.locator('textarea')).toBeDisabled()
  } finally { release() }
  await expect(page.getByText('Retry the operation')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Repetir comprobaciones' })).toBeEnabled()
})

test('publication locks pending chat edits and recovers them after an error', async ({ page }) => {
  const { release, writes } = await preparedDraft(page)
  try {
    await page.locator('textarea').fill('Change the threshold only after publication.')
    await page.getByRole('button', { name: 'Publicar', exact: true }).click()
    await expect.poll(() => writes.length).toBe(1)
    await expect(page.getByRole('button', { name: 'Enviar', exact: true })).toBeDisabled()
    await expect(page.locator('textarea')).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Repetir comprobaciones' })).toBeDisabled()
  } finally { release() }
  await expect(page.getByText('Retry the operation')).toBeVisible()
  await expect(page.locator('textarea')).toHaveValue('Change the threshold only after publication.')
  await expect(page.getByRole('button', { name: 'Enviar', exact: true })).toBeEnabled()
})

test('proposal review exposes field readers and warns when passing examples cannot test document reading', async ({ page }) => {
  const { release } = await preparedDraft(page)
  const readers = page.getByRole('list', { name: 'Lectura de inputs' })
  await expect(readers).toContainText('estimated_cost · Sin lectura automática')
  await expect(readers).toContainText('copies · Documento')
  await expect(readers).toContainText('Etiquetas: copies, Copies requested')
  await expect(readers.getByRole('status')).toHaveCount(1)
  await expect(page.getByText('estimated_cost: lectura automática desactivada')).toBeVisible()
  await expect(page.getByText('Los ejemplos comprueban valores ya aportados.', { exact: false })).toBeVisible()
  // External inputs remain legitimate: a warning does not prohibit explicit approval.
  await expect(page.getByRole('button', { name: 'Publicar', exact: true })).toBeEnabled()
  release()
})

test('chat publication exposes historical cases that could not be evaluated', async ({ page }) => {
  const { release } = await preparedDraft(page, 'incomplete')
  await expect(page.getByText('Ninguna decisión histórica pudo evaluarse por completo')).toBeVisible()
  await expect(page.getByText('0 de 3 decisiones históricas evaluadas por completo; 3 quedaron incompletas.')).toBeVisible()
  await page.getByText('Revisar casos sin cobertura completa').click()
  await expect(page.getByText('library-boundary.html: faltan estimated_cost, copies')).toBeVisible()
  release()
})

test('chat publication warns when older previews have no coverage metadata', async ({ page }) => {
  const { release } = await preparedDraft(page, 'absent')
  await expect(page.getByText('Cobertura histórica no disponible')).toBeVisible()
  release()
})
