// Run with Playwright MCP browser_run_code_unsafe(filename=...) after navigating
// to the deployed/candidate application. All business writes are blocked.
async (page) => {
  const base = page.url().replace(/\/(?:processes|docs|settings)(?:\/.*|\?.*)?$/, '/').replace(/\/$/, '')
  const results = []
  const blockedWrites = []
  const pageErrors = []
  const onError = error => pageErrors.push(error.message)
  page.on('pageerror', onError)
  const guard = async route => {
    const request = route.request()
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method()) && !request.url().endsWith('/api/login')) {
      blockedWrites.push({ method: request.method(), url: request.url() })
      return route.abort('blockedbyclient')
    }
    return route.continue()
  }
  const record = (name, pass, evidence) => results.push({ name, pass, evidence })
  const visit = async path => {
    await page.goto(`${base}${path}`)
    await page.waitForTimeout(1700)
  }
  await page.route('**/api/**', guard)
  try {
    await page.setViewportSize({ width: 1280, height: 900 })
    await visit('/processes/1/instances')
    const initial = await page.locator('body').innerText()
    record('decided document does not claim no decision exists', !initial.includes('El proceso todavía no ha emitido una decisión.'), initial.slice(-1700))
    await page.getByPlaceholder('Nombre del archivo…').fill('scan_029')
    await page.waitForTimeout(800)
    const filtered = await page.locator('body').innerText()
    record('filename filter selects matching trace', filtered.includes('1 / 540') && filtered.includes('MISSING_DATA:'), filtered.slice(-1700))
    await page.getByPlaceholder('Nombre del archivo…').fill('audit-no-such-document-20261018')
    await page.waitForTimeout(800)
    const empty = await page.locator('body').innerText()
    record('empty filter clears previous detail', empty.includes('Nada coincide.') && !empty.includes('MISSING_DATA:'), empty.slice(-700))

    await page.setViewportSize({ width: 390, height: 844 })
    await visit('/processes/1/chat')
    const geometry = await page.locator('textarea').evaluate(element => {
      const rect = element.getBoundingClientRect()
      return { left: rect.left, right: rect.right, viewport: innerWidth }
    })
    record('mobile chat composer fits viewport', geometry.left >= 0 && geometry.right <= geometry.viewport, geometry)
    const send = await page.getByRole('button', { name: 'Enviar', exact: true }).boundingBox()
    record('mobile send button fits viewport', Boolean(send && send.x >= 0 && send.x + send.width <= 390), send)
    await visit('/docs')
    const width = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth }))
    record('mobile docs avoids page horizontal overflow', width.document <= width.viewport, width)

    await page.setViewportSize({ width: 1280, height: 900 })
    const syntheticOutage = route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ code: 'audit_unavailable', message: 'Synthetic source outage' }) })
    await page.route('**/api/processes/1/sources', syntheticOutage)
    try {
      await visit('/processes/1/definition/fuentes')
      await page.waitForTimeout(6500)
      const outage = await page.locator('body').innerText()
      record('source outage visible rather than false empty state', outage.includes('Synthetic source outage') && !outage.includes('Sin fuentes. Sin ellas'), outage.slice(-1800))
    } finally {
      await page.unroute('**/api/processes/1/sources', syntheticOutage)
    }
    record('no browser runtime errors', pageErrors.length === 0, pageErrors)
    record('no attempted business mutations', blockedWrites.length === 0, blockedWrites)
    return { base, results, passed: results.filter(result => result.pass).length, total: results.length }
  } finally {
    page.off('pageerror', onError)
    await page.unroute('**/api/**', guard)
  }
}
