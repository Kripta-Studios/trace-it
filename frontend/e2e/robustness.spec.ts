import { expect, test } from '@playwright/test'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { highlight } from '../src/lib/highlight'
import { dayKey, parseDay, triage } from '../src/lib/urgency'
import type { InstanceOut } from '../src/api/contracts'

test('invoice terms preserve calendar dates across both daylight-saving transitions', () => {
  const previous = process.env.TZ
  process.env.TZ = 'Europe/Madrid'
  try {
    for (const [issued, due] of [['2026-10-01', '2026-10-31'], ['2026-03-01', '2026-03-31']]) {
      const [entry] = triage([{ id: 1, values: { date: issued, total: '' } } as InstanceOut], parseDay(due)!)
      expect(dayKey(entry.due!)).toBe(due)
      expect(entry.due!.getHours()).toBe(0)
      expect(entry.daysLeft).toBe(0)
      expect(entry.amount).toBeNull()
    }
    expect(parseDay('2026-02-31')).toBeNull()
    expect(parseDay('2026-10-01invalid')).toBeNull()
    expect(parseDay('0099-01-01')!.getFullYear()).toBe(99)
  } finally {
    if (previous === undefined) delete process.env.TZ
    else process.env.TZ = previous
  }
})

test('documentation stays inside the mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route('**/api/**', route => route.request().url().includes('/src/') ? route.continue() : route.fulfill({ json: route.request().url().endsWith('/auth-config') ? { mode: 'identity', session_hours: 12 } : [] }))
  await page.goto('docs/motor')
  await expect(page.getByRole('heading', { name: 'Cinco decisiones.' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
})

test('both themes resolve icons within the deployment prefix', async ({ page, request }) => {
  await page.route('**/api/**', route => route.request().url().includes('/src/') ? route.continue() : route.fulfill({ json: route.request().url().endsWith('/auth-config') ? { mode: 'identity', session_hours: 12 } : [] }))
  for (const theme of ['light', 'dark']) {
    await page.addInitScript(value => localStorage.setItem('trace.tema', value), theme)
    await page.goto('docs/motor')
    for (const rel of ['icon', 'apple-touch-icon']) {
      const href = await page.locator(`link[rel="${rel}"]`).getAttribute('href')
      expect(href).toContain('/nexia/trace-it/')
      const result = await request.get(new URL(href!, page.url()).href)
      expect(result.status()).toBe(200)
      expect(result.headers()['content-type']).toContain('image/png')
    }
  }
})


test('document text search handles thousands of matches without exhausting the stack', () => {
  const text = 'Abc '.repeat(15_000)
  const html = renderToStaticMarkup(createElement('pre', null, highlight(text, 'aBc')))
  expect(html.match(/<mark>/g)?.length).toBe(15_000)
  expect(html.replace(/<[^>]*>/g, '')).toBe(text)
  expect(highlight(text, '  ')).toBe(text)
  expect(highlight('Invoice [123]', '[123]')).toBeTruthy()
})
