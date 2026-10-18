import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'trace.tema'

function stored(): Theme {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function paint(theme: Theme) {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
  const favicon = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
  const touchIcon = document.querySelector<HTMLLinkElement>('link[rel="apple-touch-icon"]')
  if (favicon) favicon.href = `${import.meta.env.BASE_URL}${theme === 'dark' ? 'favicon-dark.png' : 'favicon.png'}`
  if (touchIcon) touchIcon.href = `${import.meta.env.BASE_URL}${theme === 'dark' ? 'apple-touch-icon-dark.png' : 'apple-touch-icon.png'}`
}

type ThemeState = {
  theme: Theme
  setTheme: (theme: Theme) => void
}

const Ctx = createContext<ThemeState | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const next = stored()
    paint(next)
    return next
  })

  const setTheme = useCallback((next: Theme) => {
    paint(next)
    setThemeState(next)
    try {
      if (next === 'light') window.localStorage.removeItem(STORAGE_KEY)
      else window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* private mode */
    }
  }, [])

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useTheme(): ThemeState {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useTheme fuera de ThemeProvider')
  return ctx
}
