import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useSearchParams } from 'react-router'
import { useSession } from '../state/session'
import { Button } from '../components/shell/Controls'
import { TraceMark } from '../components/shell/TraceMark'
import { ErrorNotice } from '../components/shell/Notice'

export function Login() {
  const { user, loading, mode, identityError, signIn, restoreDefaultIdentity } = useSession()
  const [params] = useSearchParams()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const requested = params.get('next') ?? '/processes'
  const next = /^\/processes(?:\/|\?|$)/.test(requested) || /^\/settings(?:\?|$)/.test(requested) ? requested : '/processes'
  if (user) return <Navigate to={location.pathname === '/login' ? next : '/processes'} replace />
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null)
    try { await signIn(email.trim(), password) }
    catch { setError(new Error('No se pudo iniciar sesión. Comprueba el correo y la contraseña e inténtalo de nuevo.')) }
    finally { setBusy(false); setPassword('') }
  }
  return (
    <main className="grid min-h-dvh place-items-center bg-canvas px-5 py-10">
      <section className="w-full max-w-md rounded-[24px] bg-shell p-7 shadow-shell ring-1 ring-line sm:p-10">
        <div className="mb-8 flex items-center gap-2 text-lg font-medium"><TraceMark className="h-8 w-8" />trace[.]it</div>
        <h1 className="text-2xl font-medium tracking-tight">Tu espacio de trabajo</h1>
        <p className="mt-2 text-sm leading-6 text-muted">Procesos, decisiones y documentación de tu empresa.</p>
        {loading ? <p role="status" className="mt-6 text-sm">Verificando acceso…</p> : identityError ? (
          <div className="mt-6"><ErrorNotice error={identityError} action={<Button onClick={restoreDefaultIdentity}>{mode === 'identity' ? 'Usar identidad predeterminada' : 'Reintentar'}</Button>} /></div>
        ) : mode === 'password' ? (
          <form onSubmit={submit} className="mt-7 space-y-5">
            <label className="block text-sm">Correo electrónico<input autoFocus required type="email" autoComplete="username" value={email} onChange={e => setEmail(e.target.value)} className="mt-2 block w-full rounded-xl border border-line bg-canvas px-3 py-2.5" /></label>
            <label className="block text-sm">Contraseña<input required type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} className="mt-2 block w-full rounded-xl border border-line bg-canvas px-3 py-2.5" /></label>
            {error ? <ErrorNotice error={error} /> : null}
            <Button type="submit" disabled={busy} className="w-full justify-center">{busy ? 'Entrando…' : 'Entrar'}</Button>
            <p className="text-xs leading-5 text-muted">Solicita tu cuenta o la recuperación de contraseña a la persona que administra este espacio.</p>
          </form>
        ) : null}
      </section>
    </main>
  )
}
