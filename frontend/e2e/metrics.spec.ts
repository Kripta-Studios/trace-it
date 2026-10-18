import { expect, test, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'

// Browser interaction fixtures; accounting and time reconciliation use real PostgreSQL
// in backend/app/features/traces/tests/test_breakdown.py.
const zero = {
  spans: 0,
  imported_spans: 0,
  errors: 0,
  requests: 0,
  replays: 0,
  input_tokens: 0,
  output_tokens: 0,
  cached_tokens: 0,
  known_cost_usd: 0,
  unpriced_requests: 0,
  timed_spans: 0,
  self_ms: 0,
  p50_ms: null,
  p95_ms: null,
}
const agent = {
  ...zero,
  spans: 26,
  imported_spans: 3,
  requests: 26,
  input_tokens: 2600,
  output_tokens: 520,
  cached_tokens: 1560,
  known_cost_usd: 2.5,
  unpriced_requests: 1,
  timed_spans: 26,
  self_ms: 26000,
  p50_ms: 1000,
  p95_ms: 1000,
}
const until = '2026-01-04T12:00:00.000Z'

async function fixture(page: Page) {
  await page.clock.setFixedTime(new Date(until))
  const state = { fail: false, empty: false, requests: [] as URL[] }
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/src/')) return route.continue()
    const path = url.pathname.split('/api')[1]
    const process = {
      id: 1,
      name: 'Metrics browser fixture',
      description: '',
      decision_types: [],
      symbols: [],
    }
    let body: unknown = []
    if (path === '/auth-config') return route.fulfill({ json: { mode: 'identity', session_hours: 12 } })
    if (path === '/login')
      body = { id: 1, email: 'metrics@test.invalid', name: 'Metrics reviewer', role: 'manager' }
    if (path === '/processes') body = [process]
    if (path === '/processes/1') body = process
    if (path === '/processes/1/summary') body = { queue: 0 }
    if (path?.includes('/mail-ingestion/activity'))
      body = { initialized: true, latest_id: 0, items: [], has_more: false }
    if (path === '/health') body = { status: 'ok' }
    if (path === '/traces/test-trace')
      body = [
        {
          id: 1,
          span_id: 'span-1',
          trace_id: 'test-trace',
          parent_id: null,
          step: 'llm_run',
          status: 'ok',
          started_at: '2026-01-04T10:00:00Z',
          duration_ms: 1000,
          data: { model: 'test-model', input_tokens: 100, cost_usd: 0.1 },
          children: [],
        },
      ]
    if (path === '/processes/1/metrics/breakdown') {
      state.requests.push(url)
      if (state.fail)
        return route.fulfill({
          status: 503,
          json: { code: 'unavailable', message: 'Metrics temporarily unavailable' },
        })
      const plane = url.searchParams.get('plane')
      const module = url.searchParams.get('module')
      const offset = Number(url.searchParams.get('offset') ?? 0)
      const groups = !plane
        ? [
            {
              ...zero,
              key: 'ingestion',
              plane: 'ingestion',
              module: null,
              model: null,
              provider: null,
            },
            { ...agent, key: 'agents', plane: 'agents', module: null, model: null, provider: null },
            {
              ...zero,
              key: 'execution',
              plane: 'execution',
              module: null,
              model: null,
              provider: null,
            },
          ]
        : [
            {
              ...agent,
              key: module ? '[null,"test-model"]' : 'agent:compiler',
              plane,
              module: 'agent:compiler',
              model: module ? 'test-model' : null,
              provider: null,
            },
          ]
      body = {
        process_id: 1,
        through_id: 26,
        plane,
        module,
        since: url.searchParams.get('since'),
        until: url.searchParams.get('until') ?? until,
        bucket_seconds: 86400,
        totals: plane ? (state.empty ? zero : agent) : null,
        groups: state.empty ? groups.map((g) => ({ ...g, ...zero })) : groups,
        flow: state.empty
          ? []
          : [
              {
                ...agent,
                key: 'compiler-test-model',
                plane: 'agents',
                module: 'agent:compiler',
                provider: 'test-provider',
                model: 'test-model',
              },
            ],
        series: state.empty
          ? []
          : ['2026-01-02T00:00:00Z', '2026-01-04T00:00:00Z'].map((started_at, i) => ({
              ...agent,
              known_cost_usd: i ? 1.5 : 1,
              started_at,
              plane: 'agents',
            })),
        activity_total: module ? 26 : 0,
        offset,
        limit: 25,
        activity:
          module && !state.empty
            ? Array.from({ length: Math.min(25, 26 - offset) }, (_, i) => ({
                ...agent,
                id: offset + i + 1,
                span_id: `span-${offset + i + 1}`,
                trace_id: 'test-trace',
                step: 'llm_run',
                status: 'ok',
                started_at: '2026-01-04T10:00:00Z',
                duration_ms: 1000,
                self_ms: 1000,
                requests: 1,
                spans: 1,
                input_tokens: 100,
                output_tokens: 20,
                cached_tokens: 60,
                known_cost_usd: offset + i === 25 ? 0 : 0.1,
                unpriced_requests: offset + i === 25 ? 1 : 0,
                cost_status: offset + i === 25 ? 'unknown' : 'known',
                model: 'test-model',
                provider: null,
                rule_id: null,
                instance_id: null,
              }))
            : [],
      }
    }
    await route.fulfill({ json: body })
  })
  return state
}

test('metrics drill down, paginate, inspect traces and preserve navigation', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await fixture(page)
  await page.goto('processes/1/metrics?hardcoded=false')
  await expect(page.getByRole('heading', { name: 'Mapa de consumo' })).toBeVisible()
  await page.getByRole('button', { name: /^Agentes:/ }).click()
  await page
    .getByRole('region', { name: 'Dónde se concentra el consumo' })
    .getByRole('button', { name: /^Compilador de reglas/ })
    .click()
  await expect(page).toHaveURL(/module=agent%3Acompiler/)
  await expect(page.getByRole('heading', { name: 'Operaciones individuales' })).toBeVisible()
  await page.getByRole('button', { name: 'Siguiente', exact: true }).click()
  await expect(
    page.getByRole('button', { name: 'Inspeccionar operación #26', exact: true }),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: 'Siguiente', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Anterior', exact: true }).click()
  await page.getByRole('button', { name: 'Inspeccionar operación #1', exact: true }).click()
  const detail = page.getByRole('region', { name: 'Consumo registrado #1', exact: true })
  await expect(detail).toContainText('Entrada en caché')
  await detail.getByRole('button', { name: 'Traza completa', exact: true }).click()
  await expect(detail.locator('pre')).toContainText('cost_usd')
  await page
    .getByRole('navigation', { name: 'Vista general' })
    .getByRole('button', { name: 'Agentes', exact: true })
    .click()
  await expect(page).not.toHaveURL(/module=/)
  await page.goBack()
  await expect(page).toHaveURL(/module=agent%3Acompiler/)
  await page.reload()
  await expect(
    page.getByRole('heading', { name: 'Compilador de reglas', exact: true }),
  ).toBeVisible()
  expect(errors).toEqual([])
})

test('evolution switches metric, selects intervals and handles mobile, empty and errors', async ({
  page,
}) => {
  const state = await fixture(page)
  await page.goto('processes/1/metrics?hardcoded=false&plane=agents&module=agent%3Acompiler')
  const controls = page.getByRole('group', { name: 'Dónde se concentra el consumo' })
  await controls.getByRole('button', { name: 'Tiempo activo', exact: true }).click()
  await expect(
    page.getByRole('img', { name: 'Evolución del consumo: Tiempo activo', exact: true }),
  ).toBeVisible()
  await controls.getByRole('button', { name: 'Tokens', exact: true }).click()
  await expect(
    page.getByRole('img', { name: 'Evolución del consumo: Tokens', exact: true }),
  ).toBeVisible()
  await page.getByRole('combobox', { name: 'Período' }).selectOption('week')
  await expect
    .poll(() => state.requests.at(-1)?.searchParams.get('since'))
    .toBe('2025-12-28T12:00:00.000Z')
  await page.getByRole('slider', { name: 'Intervalo seleccionado' }).focus()
  await page.keyboard.press('End')
  await page.keyboard.press('ArrowLeft')
  await page.getByRole('button', { name: 'Inspeccionar este intervalo' }).click()
  await expect(page.getByRole('button', { name: 'Quitar intervalo', exact: true })).toBeVisible()
  await expect
    .poll(() => state.requests.at(-1)?.searchParams.get('since'))
    .toBe('2026-01-03T00:00:00.000Z')
  await page.getByRole('button', { name: 'Quitar intervalo', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: 'Métricas', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(390)
  state.empty = true
  await page.getByRole('combobox', { name: 'Período' }).selectOption('day')
  await expect(page.getByText('No hay actividad en este período.', { exact: false })).toBeVisible()
  state.fail = true
  await page.getByRole('combobox', { name: 'Período' }).selectOption('month')
  await expect(page.getByText('Metrics temporarily unavailable', { exact: true })).toBeVisible()
  state.fail = false
  state.empty = false
  await page.getByRole('button', { name: 'Reintentar', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Evolución del consumo' })).toBeVisible()
})

test('monthly Sankey exposes task and model paths with accessible navigation', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('processes/1/metrics?period=thisMonth')
  const graph = page.getByRole('region', { name: 'Mapa de consumo', exact: true })
  await expect(page.getByRole('combobox', { name: 'Período' })).toHaveValue('thisMonth')
  const monthStart = await page.evaluate(() => new Date(2026, 0, 1).toISOString())
  await expect.poll(() => state.requests.at(-1)?.searchParams.get('since')).toBe(monthStart)
  await expect(graph.getByTestId('flow-total')).toContainText('2,50')
  await expect(graph).toContainText('Hardware y despliegue: sin registrar')
  await expect(page.getByRole('heading', { name: 'Evolución del consumo' })).not.toBeVisible()
  await page.getByText('Detalle y evolución', { exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Evolución del consumo' })).toBeVisible()
  await graph.getByRole('button', { name: 'Modelos', exact: true }).click()
  const model = graph.getByRole('button', { name: /^test-model:/ })
  await model.focus()
  await page.keyboard.press('Enter')
  const allocation = graph.getByRole('region', { name: 'Reparto por tarea' })
  await expect(allocation).toContainText('Compilador de reglas')
  await allocation.getByRole('button', { name: /^Agentes Compilador/ }).click()
  await expect(page).toHaveURL(/module=agent%3Acompiler/)
  await page.goBack()
  await graph.getByRole('button', { name: /^Compilador de reglas:/ }).click()
  await expect(page).toHaveURL(/module=agent%3Acompiler/)
  await page.goBack()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(graph.getByTestId('flow-total')).toBeVisible()
  expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(390)
  state.empty = true
  await page.getByRole('combobox', { name: 'Período' }).selectOption('day')
  await expect(graph).toContainText('No hay actividad en este período.')
  await expect(graph.getByRole('button', { name: /^Compilador de reglas:/ })).toHaveCount(0)
})

test('Sankey conserves each measure across areas, tasks and shared models', async ({ page }) => {
  await fixture(page)
  await page.goto('processes/1/metrics?hardcoded=false')
  // Bundle the real functions for this browser check: production never serves /src/*.ts.
  const built = await build({
    configFile: false, logLevel: 'silent',
    build: {
      write: false,
      lib: { entry: fileURLToPath(new URL('./fixtures/metrics-entry.ts', import.meta.url)), name: 'metricsTest', formats: ['iife'] },
    },
  })
  const output = (Array.isArray(built) ? built[0] : built).output
  const chunk = output.find((item) => item.type === 'chunk')!
  await page.addScriptTag({ content: chunk.code })
  const result = await page.evaluate(
    ({ zero }) => {
      const { usageFlow, demoBreakdown } = window.metricsTest
      const rows = [
        {
          ...zero,
          key: 'a',
          plane: 'agents',
          module: 'agent:compiler',
          provider: 'vendor',
          model: 'shared',
          known_cost_usd: 8,
          input_tokens: 80,
          self_ms: 8,
        },
        {
          ...zero,
          key: 'b',
          plane: 'agents',
          module: 'agent:tester',
          provider: 'vendor',
          model: 'small',
          known_cost_usd: 1,
          input_tokens: 10,
          self_ms: 1,
        },
        {
          ...zero,
          key: 'c',
          plane: 'ingestion',
          module: 'provider:ocr',
          provider: 'vendor',
          model: 'shared',
          known_cost_usd: 1,
          input_tokens: 10,
          self_ms: 1,
        },
        {
          ...zero,
          key: 'd',
          plane: 'ingestion',
          module: 'provider:vision',
          provider: 'vendor',
          model: 'unpriced',
          unpriced_requests: 2,
        },
      ]
      // Compare the actual ribbon endpoints, independent of the layout's sorting.
      function crossings(graph) {
        const links = graph.links.map((link) => {
          const numbers = link.path.match(/[-+]?\d*\.?\d+(?:e[-+]?\d+)?/gi).map(Number)
          const thickness = numbers[9] - numbers[7]
          return {
            column: link.source.x,
            start: numbers[1] + thickness / 2,
            end: numbers[7] + thickness / 2,
          }
        })
        return links.reduce(
          (sum, a, i) =>
            sum +
            links
              .slice(i + 1)
              .filter(
                (b) => a.column === b.column && (a.start - b.start) * (a.end - b.end) < -0.000001,
              ).length,
          0,
        )
      }
      const samples = []
      for (const measure of ['cost', 'tokens', 'time']) {
        for (const grouping of ['tasks', 'models']) {
          const graph = usageFlow(rows, measure, grouping)
          const nodes = [graph.total, ...graph.areas, ...graph.leaves]
          samples.push({
            measure,
            grouping,
            height: graph.height,
            crossings: crossings(graph),
            columns: [[graph.total], graph.areas, graph.leaves].map((col) =>
              col.reduce((sum, node) => sum + node.height, 0),
            ),
            nodes: nodes.map((node) => ({
              kind: node.kind,
              model: node.rows[0]?.model,
              height: node.height,
              expected:
                measure === 'cost'
                  ? node.totals.known_cost_usd
                  : measure === 'tokens'
                    ? node.totals.input_tokens + node.totals.output_tokens
                    : node.totals.self_ms,
              incoming: graph.links
                .filter((link) => link.target === node)
                .reduce((sum, link) => sum + link.amount, 0),
              outgoing: graph.links
                .filter((link) => link.source === node)
                .reduce((sum, link) => sum + link.amount, 0),
            })),
          })
        }
      }
      const unpriced = usageFlow([rows[3]], 'cost', 'models')
      const many = usageFlow(
        Array.from({ length: 24 }, (_, i) => ({
          ...rows[1],
          key: `task-${i}`,
          plane: i % 2 ? 'agents' : 'ingestion',
          module: `task-${i}`,
        })),
        'cost',
        'tasks',
      )
      const demo = demoBreakdown(1, {
        since: '2026-09-01T00:00:00Z',
        until: '2026-09-20T00:00:00Z',
      })
      return {
        demoCrossings: ['cost', 'tokens', 'time'].flatMap((measure) =>
          ['tasks', 'models'].map((grouping) => crossings(usageFlow(demo.flow, measure, grouping))),
        ),
        samples,
        unknown: { links: unpriced.links.length, height: unpriced.leaves[0].height },
        many: {
          leaves: many.leaves.length,
          total: many.total.totals.known_cost_usd,
          remaining: many.leaves
            .filter((node) => node.kind === 'remaining')
            .reduce((sum, node) => sum + node.totals.known_cost_usd, 0),
          crossings: crossings(many),
          leafTotal: many.leaves.reduce((sum, node) => sum + node.totals.known_cost_usd, 0),
        },
      }
    },
    { zero },
  )
  expect(result.demoCrossings).toEqual([0, 0, 0, 0, 0, 0])
  for (const graph of result.samples) {
    expect(graph.crossings).toBe(0)
    for (const column of graph.columns) expect(column).toBeCloseTo(240)
    for (const node of graph.nodes) {
      if (node.kind !== 'total') expect(node.incoming).toBeCloseTo(node.expected)
      if (node.kind === 'total' || node.kind === 'area')
        expect(node.outgoing).toBeCloseTo(node.expected)
    }
    if (graph.grouping === 'models') {
      expect(graph.nodes.filter((n) => n.kind === 'models' && n.model === 'shared')).toHaveLength(1)
    }
  }
  expect(result.unknown).toEqual({ links: 0, height: 0 })
  expect(result.many).toEqual({ leaves: 8, total: 24, remaining: 18, leafTotal: 24, crossings: 0 })
})

test('metrics always use audit data, including old example links', async ({ page }) => {
  const state = await fixture(page)
  for (const query of ['', '?hardcoded=true']) {
    await page.goto(`processes/1/metrics${query}`)
    await expect(page.getByTestId('flow-total')).toContainText('2,50')
    await expect(page.getByRole('group', { name: 'Origen de los datos' })).toHaveCount(0)
    expect(state.requests.at(-1)?.searchParams.has('since')).toBe(false)
    await expect(page.getByText('25 / 26 peticiones con precio registrado')).toBeVisible()
    await expect(page.getByText('3 operaciones restauradas', { exact: false })).toBeVisible()
  }
})
