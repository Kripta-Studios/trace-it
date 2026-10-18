import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { User } from '../api/contracts'
import { ApiError, get, post, setOnIdentityRejected, setUserId } from '../api/http'

const DEFAULT_EMAIL = import.meta.env.VITE_DEFAULT_USER_EMAIL || 'martin@trace-it.local'
const IDENTITY_KEY = 'trace.identity'
type AuthMode = 'password' | 'identity'
type Session = {
  user: User | null
  identityError: unknown
  isManager: boolean
  loading: boolean
  mode: AuthMode | null
  signIn: (email: string, password?: string) => Promise<User>
  signOut: () => Promise<void>
  restoreDefaultIdentity: () => void
}
const Ctx = createContext<Session | null>(null)
function storedEmail() {
  try { return window.sessionStorage.getItem(IDENTITY_KEY) || DEFAULT_EMAIL }
  catch { return DEFAULT_EMAIL }
}
export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [identityError, setIdentityError] = useState<unknown>(null)
  const [mode, setMode] = useState<AuthMode | null>(null)
  const [loading, setLoading] = useState(true)
  const current = useRef<User | null>(null)
  const pending = useRef<Promise<void> | null>(null)
  const queryClient = useQueryClient()
  const accept = useCallback((next: User, authMode: AuthMode) => {
    const changed = current.current?.id !== next.id
    current.current = next
    setUserId(authMode === 'identity' ? next.id : null)
    if (changed) queryClient.clear()
    setUser(next)
    setIdentityError(null)
    if (authMode === 'identity') {
      try { window.sessionStorage.setItem(IDENTITY_KEY, next.email) } catch { /* optional storage */ }
    }
    return next
  }, [queryClient])
  const clear = useCallback(() => {
    current.current = null
    setUserId(null)
    setUser(null)
    queryClient.clear()
  }, [queryClient])
  const signIn = useCallback(async (email: string, password?: string) => {
    if (!mode) throw new Error('No se ha podido verificar el acceso. Recarga la página.')
    return accept(await post<User>('/login', { email, password }), mode)
  }, [accept, mode])
  const identify = useCallback(() => {
    pending.current ??= (async () => {
      try {
        const config = await get<{ mode: AuthMode }>('/auth-config')
        if (config.mode !== 'password' && config.mode !== 'identity') throw new Error('Configuración de acceso no válida')
        setMode(config.mode)
        if (config.mode === 'password') {
          try { accept(await api.me(), config.mode) }
          catch (error) {
            if (!(error instanceof ApiError && error.status === 401)) throw error
            clear()
          }
        } else accept(await api.login(current.current?.email ?? storedEmail()), config.mode)
      } catch (error) { clear(); setIdentityError(error) }
      finally { setLoading(false) }
    })().finally(() => { pending.current = null })
    return pending.current
  }, [accept, clear])
  useEffect(() => { void identify() }, [identify])
  useEffect(() => {
    setOnIdentityRejected(() => {
      if (mode === 'password') clear()
      else if (mode === 'identity') void identify()
    })
    return () => setOnIdentityRejected(null)
  }, [mode, clear, identify])
  const signOut = useCallback(async () => {
    await post('/logout')
    clear()
    try { window.sessionStorage.removeItem(IDENTITY_KEY) } catch { /* optional storage */ }
  }, [clear])
  const restoreDefaultIdentity = useCallback(() => {
    if (mode === 'identity') void signIn(DEFAULT_EMAIL).catch(setIdentityError)
    else { setLoading(true); void identify() }
  }, [mode, signIn, identify])
  const value = useMemo(() => ({ user, identityError, isManager: user?.role === 'manager', loading, mode, signIn, signOut, restoreDefaultIdentity }), [user, identityError, loading, mode, signIn, signOut, restoreDefaultIdentity])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
export function useSession(): Session {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useSession fuera de SessionProvider')
  return ctx
}
