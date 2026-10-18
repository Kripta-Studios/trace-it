import { expect, test, type Page } from '@playwright/test'

const activeRule = {
  id: 42,
  process_id: 6,
  summary: 'Escalate severe incidents',
  text: 'Escalate an incident when its severity is at least 8.',
  decision: 'ESCALATE',
  type: 'constraint',
  status: 'active',
}

async function fixture(page: Page, conversation: 'none' | 'empty' | 'proposed') {
  const process = { id: 6, name: 'Crisis incident triage', symbols: [], decision_types: [] }
  const session = {
    id: 9,
    process_id: 6,
    published_process_id: null,
    revision: conversation === 'proposed' ? 2 : 1,
    name: process.name,
    plan: {
      ...process,
      description: '',
      connectors: [],
      sources: [],
      rules: conversation === 'proposed'
        ? [{ ...activeRule, name: 'new-threshold', summary: 'Proposed lower threshold', evidence: [] }]
        : [],
      guidance: [],
      examples: [],
      questions: [],
    },
    messages: [],
    reviews: {},
    snapshots: [],
    changes: conversation === 'proposed' ? [{ field: 'rules', before: [], after: [] }] : [],
  }
  const writes: string[] = []
  const ruleQueries: URL[] = []
  const versions = [
    {
      id: 46, number: 2, created_at: '2026-09-20T00:00:00Z', author: 'Manager', reason: 'Current policy',
      snapshot: { process: { symbols: [] }, rules: [activeRule] },
      validation: {
        unchanged: 8,
        changes: [{ instance_id: 17, name: 'incident-017.pdf', before: 'MONITOR', after: 'ESCALATE' }],
        conflicts: [{ instance_id: 18, name: 'incident-018.pdf', before: 'MONITOR', after: 'DISPATCH' }],
      },
    },
    {
      id: 44, number: 1, created_at: '2026-09-19T00:00:00Z', author: 'Manager', reason: 'Initial policy',
      snapshot: { process: { symbols: [] }, rules: [{ ...activeRule, id: 40, text: 'Previously approved threshold.' }] },
      validation: { unchanged: 3, changes: [], conflicts: [] },
    },
  ]
  const state = { rules: [{ ...activeRule }], failCompile: false, compileRequests: 0, restoredVersion: null as number | null }
  let finishCompilation = () => {}
  const compilation = new Promise<void>((resolve) => { finishCompilation = resolve })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.includes('/src/')) return route.continue()
    const path = url.pathname.split('/api')[1]
    if (request.method() !== 'GET' && path !== '/login') writes.push(path)
    let body: unknown = []
    if (path === '/login') body = { id: 1, name: 'Manager', email: 'manager@test.invalid', role: 'manager' }
    if (path === '/processes') body = [process]
    if (path === '/processes/6') body = process
    if (path === '/processes/6/summary') body = { queue: 0 }
    if (path === '/processes/6/execution') body = { revision: state.restoredVersion ? 1 : null, version_id: 46 }
    if (path === '/processes/6/versions') body = versions
    if (path === '/processes/6/findings') body = [{ id: 1, type: 'rule_change' }]
    if (path === '/processes/6/draft') {
      if (request.method() === 'PUT') state.restoredVersion = request.postDataJSON().restore_version_id
      body = { revision: 1, snapshot: {} }
    }
    if (path === '/process-drafts') body = conversation === 'none' ? [] : [session]
    if (path === '/process-drafts/9') body = session
    if (path === '/processes/6/rules') {
      ruleQueries.push(url)
      if (request.method() === 'POST') {
        const rule = { ...activeRule, id: 45, ...request.postDataJSON(), summary: null, status: 'compiling' }
        state.rules.push(rule)
        return route.fulfill({ status: 201, json: rule })
      }
      body = state.rules
    }
    if (path === '/rules/43/compile') {
      state.compileRequests++
      await compilation
      if (state.failCompile)
        return route.fulfill({ status: 502, json: { code: 'compilation_failed', message: 'Compilation unavailable' } })
      body = state.rules.find((rule) => rule.id === 43)
    }
    if (path?.includes('/mail-ingestion/activity'))
      body = { initialized: true, latest_id: 0, items: [], has_more: false }
    if (path === '/health') body = { status: 'ok' }
    await route.fulfill({ json: body })
  })
  return { writes, ruleQueries, state, finishCompilation }
}

for (const conversation of ['none', 'empty', 'proposed'] as const) {
  test(`existing process shows current rules with ${conversation} conversation`, async ({ page }) => {
    const state = await fixture(page, conversation)
    await page.goto('processes/6/chat')
    const rules = page.getByRole('region', { name: 'Process rules', exact: true })
    await expect(rules.getByRole('link', { name: activeRule.summary })).toBeVisible()
    await expect(rules).not.toContainText(activeRule.text)
    await expect(rules.getByRole('link', { name: activeRule.summary })).toHaveAttribute('title', activeRule.text)
    await expect(rules).toContainText('Activa')
    await expect(rules.getByRole('link', { name: activeRule.summary })).toHaveAttribute(
      'href', /\/processes\/6\/rules\/42$/,
    )
    // Viewing the published rules must not request approval or mutate a draft.
    await expect(rules.getByRole('button', { name: 'Compilar', exact: true })).toHaveCount(0)
    await expect(rules.getByRole('button', { name: 'Añadir regla', exact: true })).toBeVisible()
    await expect(rules).not.toContainText('Proposed lower threshold')
    expect(state.ruleQueries.length).toBeGreaterThan(0)
    expect(state.ruleQueries.every((url) => !url.searchParams.has('status'))).toBe(true)
    expect(state.writes).toEqual([])
    if (conversation === 'proposed')
      await expect(page.getByRole('button', { name: 'Aprobar Proposed lower threshold', exact: true })).toBeVisible()
  })
}

test('a new process does not show another process rules', async ({ page }) => {
  const state = await fixture(page, 'none')
  await page.goto('processes/new')
  await expect(page.getByRole('button', { name: 'Empezar conversación', exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Process rules', exact: true })).toHaveCount(0)
  expect(state.ruleQueries).toEqual([])
})

for (const path of ['chat', 'definition/manual']) {
  test(`${path} keeps rule hover, compilation, errors and creation`, async ({ page }) => {
    const { state, writes, finishCompilation } = await fixture(page, 'empty')
    state.rules.push(
      { ...activeRule, id: 43, summary: 'Draft rule', status: 'draft' },
      { ...activeRule, id: 44, summary: 'Blocked rule', status: 'blocked' },
    )
    await page.goto(`processes/6/${path}`)
    const draft = page.getByRole('listitem').filter({ has: page.getByRole('link', { name: 'Draft rule', exact: true }) })
    const compile = draft.getByRole('button', { name: 'Compilar', exact: true })
    await expect(compile).toHaveCSS('opacity', '0')
    await draft.hover()
    await expect(compile).toHaveCSS('opacity', '1')
    await expect(draft.getByText('Borrador', { exact: true })).toHaveCSS('opacity', '1')
    await compile.click()
    await expect(draft).toContainText('0s')
    await expect(compile).toHaveCount(0)
    expect(state.compileRequests).toBe(1)
    state.failCompile = true
    finishCompilation()
    await expect(page.getByText('Compilation unavailable', { exact: true })).toBeVisible()
    state.failCompile = false
    await draft.hover()
    await compile.click()
    await expect(page.getByText('Compilation unavailable', { exact: true })).toHaveCount(0)
    await expect.poll(() => state.compileRequests).toBe(2)
    const blocked = page.getByRole('listitem').filter({ has: page.getByRole('link', { name: 'Blocked rule', exact: true }) })
    await blocked.hover()
    await expect(blocked.getByRole('button', { name: 'Compilar', exact: true })).toHaveCSS('opacity', '1')
    await page.getByRole('button', { name: 'Añadir regla', exact: true }).click()
    const input = page.getByPlaceholder('Una frase. Enter guarda, Esc cancela.')
    await input.fill('Escalate incomplete incidents')
    await input.press('Enter')
    await expect(page.getByRole('link', { name: 'Escalate incomplete incidents', exact: true })).toBeVisible()
    expect(writes).toEqual(['/rules/43/compile', '/rules/43/compile', '/processes/6/rules'])
  })

  test(`${path} browses version backtesting and restores only a draft`, async ({ page }) => {
    const { state, writes } = await fixture(page, 'empty')
    await page.goto(`processes/6/${path}`)
    await page.getByRole('button', { name: 'v2', exact: true }).click()
    await expect(page.getByRole('menu')).toContainText('Impacto histórico · 1 aviso')
    await page.getByRole('menuitemradio', { name: /^v1/ }).click()
    await expect(page.getByText('Estás viendo v1, solo lectura', { exact: true })).toBeVisible()
    await expect(page.getByText('Previously approved threshold.', { exact: true })).toBeVisible()
    await expect(page.getByText('IMPACTO HISTÓRICO AL PUBLICAR', { exact: true })).toBeVisible()
    await expect(page.getByText('Ninguna decisión cambiaría con esta versión.', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Añadir regla', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'v1', exact: true }).click()
    await page.getByRole('menuitemradio', { name: /^v2/ }).click()
    await expect(page.getByText('v2 en vigor', { exact: true })).toBeVisible()
    await page.getByText('Decisiones del motor que cambiarían · 1', { exact: true }).click()
    await expect(page.getByRole('link', { name: 'incident-017.pdf', exact: true })).toBeVisible()
    await page.getByText('Decisiones protegidas que entrarían en conflicto · 1', { exact: true }).click()
    await expect(page.getByRole('link', { name: 'incident-018.pdf', exact: true })).toBeVisible()
    expect(writes).toEqual([])
    await page.getByRole('button', { name: 'Volver a la definición', exact: true }).click()
    await expect(page.getByRole('link', { name: activeRule.summary, exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'v2', exact: true }).click()
    await page.getByRole('menuitemradio', { name: /^v1/ }).click()
    await page.getByRole('button', { name: 'Restaurar como borrador', exact: true }).click()
    await expect(page.getByText('v1 restaurada como borrador', { exact: true })).toBeVisible()
    expect(state.restoredVersion).toBe(44)
    expect(writes).toEqual(['/processes/6/draft'])
  })
}

test('mobile conversation keeps the composer and actions inside the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await fixture(page, 'empty')
  await page.goto('processes/6/chat')
  const composer = page.getByPlaceholder('Pregunta algo o pide un cambio en el proceso.')
  await expect(composer).toBeVisible()
  for (const control of [composer, page.getByRole('button', { name: 'Nueva', exact: true }), page.getByRole('button', { name: 'Enviar', exact: true })]) {
    const bounds = await control.boundingBox()
    expect(bounds).not.toBeNull()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
  }
})
