/**
 * The demo path through the real console and the real API (docs/backend-plan.md, B0).
 *
 * `make e2e-integration` recreates the database and publishes the frozen pack; the config
 * starts the challenge ERP, the API and the console. No LLM key: the pack is frozen, so
 * nothing compiles, and the assistant's proposal is not part of the path (D4).
 *
 * A step waiting for one of Carlos's packages is `test.fixme('pkg n')`, and goes live in
 * the PR that lands package n (docs/frontend-handoff.md).
 *
 * reviewer-agent FE-1..3 (docs/reviewer-agent.md): opening a case sends no POST /proposal;
 * a resolved case offers "Sugerir regla"; a case no rule can learn answers 409 in Spanish;
 * a stored rule suggestion is shown, rejected with a reason, and reads `Rechazada`; another
 * is edited before Aceptar, and the created rule carries the edit (`outcome.edited`).
 */
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const API = `http://127.0.0.1:${process.env.E2E_PORTS!.split(',')[1]}`
const MANAGER = { email: 'martin@trace-it.local', name: 'Martín' }
const PROCESS = 'Invoice payment - frozen 2026-09-19'
const CHALLENGE = resolve(import.meta.dirname, '../../.context/500-sombras-de-alberto')
const INVOICES = resolve(CHALLENGE, 'facturas')
// The client's workbook and the day batch 1 arrived, as `make demo` loads them.
const WORKBOOK = 'FINAL_v7_DEFINITIVO_ahorasi.xlsx'
const CUT_OFF = '2026-09-18'
// Two text PDFs on the same order: the golden says both end ESCALAR (DUPLICATE_PO).
const ESCALATED = ['2026-0233-A_catering.pdf', 'factura_41082.pdf']
// An image-only scan, read by the local OCR weights (`make ocr-models`).
const SCAN = 'scan_001.pdf'
const RESOLVED = 'factura_41082.pdf'
const NOTE = 'Pedido duplicado revisado: esta es la factura buena'
const LEARNED = 'Cargar el maestro de proveedores actualizado'
// The reviewer agent's amended rule, stored as it would with no LLM key (D4).
const AMENDED = 'The invoice shares its order with another invoice whose total exceeds the order.'
const REJECTION = 'Un pedido duplicado siempre lo mira una persona'
// The manager's edit of a second suggestion, before Aceptar.
const EDITED = 'The invoice shares its order with another invoice and together they exceed the order total.'
const DATABASE =
  process.env.E2E_DATABASE_URL ?? 'postgresql+psycopg://trace:trace@localhost:5432/trace_e2e_test'

test.describe.configure({ mode: 'serial' })

let page: Page
let processId: number
let headers: Record<string, string>
// API responses that failed, for the error check on every screen. With no key, the
// assistant's proposal answers 502 `llm_error`; it is not part of the path (D4).
let failures: string[] = []
// reviewer-agent FE-1: the assistant is an LLM call, never made just by opening a case.
const proposalCalls: string[] = []
const ASSISTANT = /^502 (GET|POST) .*\/(suggestion|proposal)$/

test.beforeAll(async ({ browser, request }) => {
  const processes: { id: number; name: string }[] = await (await request.get(`${API}/processes`)).json()
  processId = processes.find((item) => item.name === PROCESS)!.id
  // The sources, as the real demo loads them before any invoice: a rule reading a source
  // that was never loaded escalates SOURCE_UNAVAILABLE (B11), which is not the demo.
  const manager = await (await request.post(`${API}/login`, { data: { email: MANAGER.email } })).json()
  headers = { 'X-User-Id': String(manager.id) }
  const book = await request.post(`${API}/processes/${processId}/sources/workbook`, {
    headers,
    multipart: {
      file: {
        name: WORKBOOK,
        mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        buffer: readFileSync(resolve(CHALLENGE, WORKBOOK)),
      },
      cut_off_date: CUT_OFF,
    },
  })
  expect(book.status(), await book.text()).toBe(201)
  const erp = await request.post(`${API}/processes/${processId}/sources/erp/sync`, { headers })
  expect(erp.ok(), await erp.text()).toBe(true)
  page = await browser.newPage()
  page.on('request', (request) => {
    if (request.method() === 'POST' && /\/instances\/\d+\/proposal$/.test(request.url())) {
      proposalCalls.push(request.url())
    }
  })
  page.on('response', (response) => {
    if (response.url().includes('/api/') && response.status() >= 400) {
      failures.push(`${response.status()} ${response.request().method()} ${response.url()}`)
    }
  })
})

test.afterAll(async () => {
  await page?.close()
})

/**
 * The data is real (no MOCK DATA badge) and no `ErrorNotice` is on screen, found by its
 * titles or by the "<status> · <code>" line under a backend message. `allow` lists the
 * codes one screen may show, e.g. the assistant's `llm_error` with no key.
 */
async function realAndClean(allow: string[] = []) {
  await expect(page.getByText('MOCK DATA', { exact: true })).toHaveCount(0)
  for (const title of ['El backend no responde', 'Endpoint pendiente', 'Algo ha fallado']) {
    await expect(page.getByText(title, { exact: true })).toHaveCount(0)
  }
  const codes = await page.getByText(/^\d{3} · [a-z_]+$/).allTextContents()
  expect(codes.filter((text) => !allow.some((code) => text.endsWith(` · ${code}`)))).toEqual([])
  expect(failures.filter((line) => !ASSISTANT.test(line))).toEqual([])
  failures = []
}

/** Every proposal channel is an LLM call; with no key (D4) the test stores one as it would. */
function storeProposal(kind: 'escalation' | 'source' | 'rule', target: number, value: string): number {
  const out = execFileSync(
    'uv',
    ['run', 'python', '-m', 'tests.support.proposals', kind, String(target), value],
    { cwd: resolve(import.meta.dirname, '../../backend'), env: { ...process.env, TRACE_DATABASE_URL: DATABASE } },
  )
  return Number(out.toString().trim().split('\n').at(-1))
}

async function instanceByName(request: APIRequestContext, name: string) {
  const items: { id: number; name: string }[] = await (
    await request.get(`${API}/processes/${processId}/instances`)
  ).json()
  const id = items.find((item) => item.name === name)!.id
  return (await request.get(`${API}/instances/${id}`)).json()
}

test('the console acts as the manager, with no login screen', async () => {
  await page.goto('/processes')
  await page.waitForLoadState('networkidle')
  await realAndClean()
  await expect(page).toHaveURL(/\/processes$/)
  await expect(page.getByRole('link', { name: new RegExp(MANAGER.name) })).toBeVisible()
})

test('the process inbox opens its real console panel', async () => {
  await page.getByRole('link', { name: PROCESS }).first().click()
  await expect(page).toHaveURL(new RegExp(`/processes/${processId}$`))
  await expect(page.getByText('Sin revisiones pendientes', { exact: true })).toBeVisible()
  await realAndClean()
  await page.getByRole('link', { name: 'Consola', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/processes/${processId}/panel$`))
  await expect(page.getByRole('heading', { level: 1, name: PROCESS })).toBeVisible()
  await realAndClean()
})

test('pkg 2: process panel shows the published version', async () => {
  await expect(page.getByText(/v1\b|versión 1/)).toBeVisible()
  await realAndClean()
})

test('upload two text PDFs and run', async ({ request }) => {
  await page.getByRole('button', { name: 'Ejecutar', exact: true }).click()
  await page
    .locator('input[type=file][multiple]')
    .setInputFiles(ESCALATED.map((name) => resolve(INVOICES, name)))
  await page.getByRole('button', { name: `Ejecutar ${ESCALATED.length}` }).click()
  await expect(page.getByText('Lote completado')).toBeVisible({ timeout: 120_000 })
  // The golden: both escalate because they share a purchase order (DUPLICATE_PO).
  for (const name of ESCALATED) {
    const instance = await instanceByName(request, name)
    const other = ESCALATED.find((item) => item !== name)
    expect([instance.decision, instance.reason]).toEqual(['ESCALAR', `Same order as: ${other}`])
  }
  await realAndClean()
})

// The run dialog is still open: one row per outcome, "<decision> <share>% <count>".
test('pkg 3: the run shows real progress and `by_decision`', async () => {
  // The Panel's run history repeats the split: read it inside the run dialog.
  const dialog = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: 'Lote completado' }) })
    .last()
  await expect(dialog.getByText(new RegExp(`^${ESCALATED.length}\\s*documentos decididos$`))).toBeVisible()
  await expect(dialog.getByRole('listitem')).toHaveText([
    new RegExp(`^ESCALAR\\s*100%\\s*${ESCALATED.length}$`),
  ])
})

test('upload a scan and run', async ({ request }) => {
  await page.getByRole('button', { name: 'Cerrar' }).click()
  await page.getByRole('button', { name: 'Ejecutar', exact: true }).click()
  await page.locator('input[type=file][multiple]').setInputFiles(resolve(INVOICES, SCAN))
  await page.getByRole('button', { name: 'Ejecutar 1' }).click()
  await expect(page.getByText('Lote completado')).toBeVisible({ timeout: 300_000 })
  // Local OCR alone cannot read every field of this scan, so it escalates, never guesses.
  const scan = await instanceByName(request, SCAN)
  expect(scan.decision).toBe('ESCALAR')
  expect(scan.reason).toMatch(/^MISSING_DATA: /)
  await page.getByRole('button', { name: 'Cerrar' }).click()
  await realAndClean()
})

test('the queue shows the escalated invoices', async () => {
  await page.goto(`/processes/${processId}/review`)
  await expect(page.getByRole('button', { name: /^ESCALAR \d+$/ })).toBeVisible()
  for (const name of [...ESCALATED, SCAN]) {
    await expect(page.getByRole('button', { name })).toBeVisible()
  }
  await realAndClean(['llm_error'])
})

test('open the escalation detail', async () => {
  await page.getByRole('button', { name: RESOLVED }).click()
  await expect(page.getByRole('link', { name: 'Ver traza →' })).toBeVisible()
  await expect(page.getByRole('combobox', { name: 'Decisión' })).toBeVisible()
  // FE-1: the assistant waits behind its button; opening the case asked it nothing.
  await expect(page.getByRole('button', { name: 'Pedir propuesta al asistente' })).toBeVisible()
  expect(proposalCalls).toEqual([])
  // Only final decision types can be chosen by hand.
  const options = page.getByRole('combobox', { name: 'Decisión' }).locator('option')
  expect(await options.allTextContents()).not.toContain('ESCALAR')
  await realAndClean(['llm_error'])
})

test('pkg 5: the detail says why it escalated, as the API does', async ({ request }) => {
  const instance = await instanceByName(request, RESOLVED)
  await expect(page.getByRole('heading', { name: 'Motivo de revisión' })).toBeVisible()
  await page.getByText('Detalle del motor', { exact: true }).click()
  // Exact: the rule result below also quotes the reason.
  await expect(page.getByText(instance.reason, { exact: true })).toBeVisible()
  await realAndClean(['llm_error'])
})

test('pkg 5: accept the assistant proposal', async ({ request }) => {
  // On the scan: the next steps resolve RESOLVED by hand and read ESCALATED[0] as escalated.
  storeProposal('escalation', (await instanceByName(request, SCAN)).id, 'NO_PAGAR')
  await page.getByRole('button', { name: SCAN }).click()
  const card = page.locator('div').filter({ has: page.getByText('El asistente propone') }).last()
  await expect(card.getByText('Stored by the e2e: no LLM key')).toBeVisible()
  await card.getByRole('button', { name: 'Aceptar' }).click()
  await expect(page.getByRole('button', { name: SCAN })).toHaveCount(0)
  const scan = await instanceByName(request, SCAN)
  expect(scan.decision).toBe('NO_PAGAR')
  expect(scan.decisions.at(-1).author).not.toBe('engine')
  // Back on the case the next step resolves by hand.
  await page.getByRole('button', { name: RESOLVED }).click()
  await expect(page.getByRole('combobox', { name: 'Decisión' })).toBeVisible()
  await realAndClean(['llm_error'])
})

test('resolve as the manager', async ({ request }) => {
  await page.getByRole('combobox', { name: 'Decisión' }).selectOption('PAGAR')
  await page.getByRole('textbox', { name: /^Motivo/ }).fill(NOTE)
  await page.getByRole('button', { name: 'Resolver', exact: true }).click()
  // Resolved, the case leaves the ESCALAR list, but stays open with the reviewer's nudge.
  await expect(page.getByRole('button', { name: RESOLVED })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Sugerir regla' })).toBeVisible()
  const instance = await instanceByName(request, RESOLVED)
  expect(instance.decision).toBe('PAGAR')
  expect(instance.decisions.at(-1)).toMatchObject({ decision: 'PAGAR', reason: NOTE })
  expect(instance.decisions.at(-1).author).not.toBe('engine')
  expect(proposalCalls).toEqual([])
  await realAndClean(['llm_error'])
})

test('reviewer agent: a case no rule can learn answers in Spanish', async ({ request }) => {
  // The scan escalated MISSING_DATA and was resolved above: the gate answers 409, no model.
  const scan = await instanceByName(request, SCAN)
  await page.goto(`/processes/${processId}/review?i=${scan.id}`)
  await page.getByRole('button', { name: 'Sugerir regla' }).click()
  await expect(page.getByText('Ninguna regla puede aprender este caso')).toBeVisible()
  await expect(page.getByText(/^Faltaba un dato obligatorio .*ninguna regla puede suplir un dato\.$/)).toBeVisible()
  // The 409 is the answer here, not a failure.
  failures = failures.filter((line) => !/^409 POST .*\/rule-proposal$/.test(line))
  await realAndClean(['llm_error'])
})

test('reviewer agent: a rule suggestion is shown, then rejected with a reason', async ({ request }) => {
  const resolved = await instanceByName(request, RESOLVED)
  const id = storeProposal('rule', resolved.id, AMENDED)
  // Reopening the resolved case finds its suggestion.
  await page.goto(`/processes/${processId}/review?i=${resolved.id}`)
  await expect(page.getByText(AMENDED)).toBeVisible()
  await expect(page.getByRole('link', { name: /^Sustituye a la regla \d+/ })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sugerir regla' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Rechazar' }).click()
  await page.getByPlaceholder('Por qué no').fill(REJECTION)
  await page.getByRole('button', { name: 'Rechazar' }).click()
  await expect(page.getByText('Rechazada', { exact: true })).toBeVisible()
  await expect(page.getByText(`Motivo: ${REJECTION}`)).toBeVisible()
  const rejected: { id: number; outcome: { reason?: string } }[] = await (
    await request.get(`${API}/processes/${processId}/proposals?status=rejected`, { headers })
  ).json()
  expect(rejected.find((item) => item.id === id)?.outcome).toMatchObject({ reason: REJECTION })
  // Rejected, the manager may ask again.
  await expect(page.getByRole('button', { name: 'Sugerir regla' })).toBeVisible()
  expect(proposalCalls).toEqual([])
  await realAndClean(['llm_error'])
})

test('trace view shows the decisions and the escalation reason', async ({ request }) => {
  await page.getByRole('link', { name: 'Ejecuciones' }).click()
  await page.getByRole('button', { name: new RegExp(RESOLVED.replace('.', '\\.')) }).click()
  await expect(page.getByText('Decisión final')).toBeVisible()
  await expect(page.getByText(NOTE).first()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Historial de decisiones 2', exact: true })).toBeVisible()

  // The one still escalated: its reason on screen is the one the API stored.
  const other = await instanceByName(request, ESCALATED[0])
  await page.getByRole('button', { name: new RegExp(ESCALATED[0].replace('.', '\\.')) }).click()
  await expect(page.getByText(other.reason, { exact: true }).first()).toBeVisible()
  await realAndClean()
})

test('pkg 8: accept a learning proposal from the inbox', async ({ request }) => {
  const id = storeProposal('source', processId, LEARNED)
  await page.goto(`/processes/${processId}/definition`)
  const card = page.getByRole('listitem').filter({ hasText: LEARNED })
  await card.getByRole('button', { name: 'Aceptar' }).click()
  // Settled, it leaves the open inbox.
  await expect(card).toHaveCount(0)
  const accepted: { id: number }[] = await (
    await request.get(`${API}/processes/${processId}/proposals?status=accepted`, { headers })
  ).json()
  expect(accepted.map((item) => item.id)).toContain(id)
  await realAndClean()
})

test('pkgs 9, 11: definition and settings screens load real data', async () => {
  for (const path of ['definition', 'definition/contexto', 'definition/inputs', 'definition/fuentes', 'settings']) {
    await page.goto(`/processes/${processId}/${path}`)
    await page.waitForLoadState('networkidle')
    await realAndClean()
  }
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')
  await realAndClean()
})

test('pkg 7: run history lists the run', async () => {
  await page.goto(`/processes/${processId}/panel`)
  // Two runs: the text PDFs, then the scan. The older one lists its own cases.
  const runs = page.getByRole('link', { name: /decisiones · v1/ })
  await expect(runs).toHaveCount(2)
  await runs.last().click()
  await expect(page).toHaveURL(/\?run=\d+/)
  await expect(page.getByText(/^Ejecución del .* · v1$/)).toBeVisible()
  for (const name of ESCALATED) {
    await expect(page.getByRole('button', { name: new RegExp(name.replace('.', '\\.')) })).toBeVisible()
  }
  await page.goBack()
  await expect(page).toHaveURL(new RegExp(`/processes/${processId}/panel$`))
  await realAndClean()
})

// Last: accepting stages a rule in the draft, which the earlier screens do not expect.
test('reviewer agent: the manager edits the suggested rule, then accepts it', async ({ request }) => {
  const resolved = await instanceByName(request, RESOLVED)
  const id = storeProposal('rule', resolved.id, AMENDED)
  await page.goto(`/processes/${processId}/review?i=${resolved.id}`)
  const text = page.getByRole('textbox', { name: /^Regla que entra con esta decisión/ })
  await expect(text).toHaveValue(AMENDED)
  await text.fill(EDITED)
  await page.getByRole('button', { name: 'Aceptar' }).click()
  await expect(page.getByText('Aceptada', { exact: true })).toBeVisible()
  await expect(page.getByText(`Editada por ti. El agente proponía: ${AMENDED}`)).toBeVisible()
  // Staged in the draft, not published.
  await expect(page.getByRole('link', { name: 'Panel → Publicar' })).toBeVisible()
  const accepted: { id: number; outcome: { edited?: boolean; original_text?: string; rule_id: number } }[] =
    await (await request.get(`${API}/processes/${processId}/proposals?status=accepted`, { headers })).json()
  const outcome = accepted.find((item) => item.id === id)!.outcome
  expect(outcome).toMatchObject({ edited: true, original_text: AMENDED })
  const rule = await (await request.get(`${API}/rules/${outcome.rule_id}`)).json()
  expect(rule.text).toBe(EDITED)
  expect(proposalCalls).toEqual([])
  await realAndClean(['llm_error'])
})
