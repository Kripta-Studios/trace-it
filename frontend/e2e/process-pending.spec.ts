import { expect, test } from '@playwright/test'

test('process rows show only pending human reviews and open their inbox', async ({ page }) => {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/src/')) return route.continue()
    const path = url.pathname.split('/api')[1]
    let body: unknown = []
    if (path === '/auth-config') return route.fulfill({ json: { mode: 'identity', session_hours: 12 } })
    if (path === '/login') body = { id: 1, name: 'Manager', email: 'test@ci.invalid', role: 'manager' }
    if (path === '/processes') body = [
      { id: 1, name: 'Invoices', description: 'Review invoice payments', use_case_id: 1 },
      { id: 2, name: 'Expenses', description: '', use_case_id: 2 },
    ]
    if (path === '/processes/1/summary') body = { queue: 3 }
    if (path === '/processes/2/summary') body = { queue: 0, by_status: { PENDING: 12 } }
    if (path?.includes('/mail-ingestion/activity')) body = { initialized: true, latest_id: 0, items: [], has_more: false }
    await route.fulfill({ json: body })
  })
  await page.goto('processes')
  const list = page.getByRole('main').getByRole('list')
  const invoices = list.getByRole('link', { name: /Invoices/ })
  await expect(invoices.getByLabel(/3 (pending reviews|revisiones pendientes)/)).toBeVisible()
  await expect(list.getByRole('link', { name: 'Expenses', exact: true })).toBeVisible()
  await expect(invoices).toHaveAttribute('href', /\/processes\/1$/)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(invoices.getByLabel(/3 (pending reviews|revisiones pendientes)/)).toBeVisible()
  expect(await page.getByRole('main').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
})
