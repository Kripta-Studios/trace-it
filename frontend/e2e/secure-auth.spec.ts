import { expect, test } from '@playwright/test'

test('password login preserves deep links, survives reload, removes identity switch and logs out', async ({ page }) => {
  let signedIn = false
  const logins: unknown[] = []
  let businessBeforeLogin = 0
  const user = { id: 1, name: 'Private manager', email: 'owner@example.test', role: 'manager' }
  await page.route('**/api/**', async route => {
    const req = route.request()
    if (req.url().includes('/src/')) return route.continue()
    const path = new URL(req.url()).pathname.split('/api')[1]
    if (path === '/auth-config') return route.fulfill({ json: { mode: 'password', session_hours: 12 } })
    if (path === '/me') return route.fulfill({ status: signedIn ? 200 : 401, json: signedIn ? user : { detail: 'Authentication required' } })
    if (path === '/login') {
      const body = req.postDataJSON(); logins.push(body)
      signedIn = body.password === 'correct-password'
      return route.fulfill({ status: signedIn ? 200 : 401, json: signedIn ? user : { detail: 'Invalid credentials' } })
    }
    if (path === '/logout') { signedIn = false; return route.fulfill({ status: 204 }) }
    if (!signedIn) businessBeforeLogin++
    if (path === '/health') return route.fulfill({ json: { status: 'ok' } })
    return route.fulfill({ json: [] })
  })
  await page.goto('settings?tab=identity')
  await expect(page.getByRole('heading', { name: 'Tu espacio de trabajo' })).toBeVisible()
  expect(logins).toEqual([])
  expect(businessBeforeLogin).toBe(0)
  await page.getByLabel('Correo electrónico').fill(user.email)
  await page.getByLabel('Contraseña', { exact: true }).fill('wrong-password')
  await page.getByRole('button', { name: 'Entrar', exact: true }).click()
  await expect(page.getByText('No se pudo iniciar sesión.', { exact: false })).toBeVisible()
  await page.getByLabel('Contraseña', { exact: true }).fill('correct-password')
  await page.getByRole('button', { name: 'Entrar', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Tu cuenta' })).toBeVisible()
  await expect(page.getByText(user.email, { exact: false })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: 'Cerrar sesión' })).toBeVisible()
  await page.getByRole('button', { name: 'Cerrar sesión' }).click()
  await expect(page.getByRole('heading', { name: 'Tu espacio de trabajo' })).toBeVisible()
  await expect(page.getByText(user.email, { exact: false })).toHaveCount(0)
  expect(logins).toHaveLength(2)
})

test('unavailable authentication configuration fails closed', async ({ page }) => {
  const calls: string[] = []
  await page.route('**/api/**', route => {
    if (route.request().url().includes('/src/')) return route.continue()
    calls.push(new URL(route.request().url()).pathname)
    return route.fulfill({ status: 503, json: { detail: 'Access service unavailable' } })
  })
  await page.goto('processes')
  await expect(page.getByRole('button', { name: 'Reintentar' })).toBeVisible()
  expect(calls.every(path => path.endsWith('/auth-config'))).toBe(true)
  await expect(page.getByRole('button', { name: 'Entrar', exact: true })).toHaveCount(0)
})
