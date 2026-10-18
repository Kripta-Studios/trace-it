import { useEffect, useRef, type ReactNode } from 'react'
import { motion } from 'motion/react'

const ease = [0.23, 1, 0.32, 1] as const

/** Open overlays, innermost last: Escape closes only the one on top. */
const stack: symbol[] = []
let originalOverflow = ''
const focusable = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'

export function Overlay({
  onClose,
  children,
  align = 'center',
  size = 'md',
  label,
}: {
  onClose: () => void
  children: ReactNode
  align?: 'center' | 'right'
  size?: 'md' | 'lg' | 'xl'
  label?: string
}) {
  const close = useRef(onClose)
  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    close.current = onClose
  })
  useEffect(() => {
    const id = Symbol('overlay')
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    if (stack.length === 0) {
      originalOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
    stack.push(id)
    const controls = () => Array.from(panel.current?.querySelectorAll<HTMLElement>(focusable) ?? [])
      .filter(element => !element.matches(':disabled, [hidden], [aria-hidden="true"]') && element.tabIndex >= 0 && element.getClientRects().length > 0)
    ;(controls()[0] ?? panel.current)?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (stack.at(-1) !== id) return
      if (event.key === 'Escape') {
        event.preventDefault()
        close.current()
      }
      if (event.key === 'Tab') {
        const items = controls()
        const first = items[0]
        const last = items.at(-1)
        if (!first) {
          event.preventDefault()
          panel.current?.focus()
        } else if (event.shiftKey && (document.activeElement === first || !items.includes(document.activeElement as HTMLElement))) {
          event.preventDefault()
          last?.focus()
        } else if (!event.shiftKey && (document.activeElement === last || !items.includes(document.activeElement as HTMLElement))) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      const wasTop = stack.at(-1) === id
      stack.splice(stack.indexOf(id), 1)
      if (stack.length === 0) document.body.style.overflow = originalOverflow
      if (wasTop && previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  return (
    <div
      className={
        align === 'right'
          ? 'fixed inset-0 z-50 flex justify-end bg-scrim p-3'
          : 'fixed inset-0 z-50 grid place-items-center bg-scrim p-4'
      }
      onClick={onClose}
    >
      <motion.div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        initial={
          align === 'right'
            ? { x: 24, opacity: 0.6 }
            : { y: 8, opacity: 0, scale: 0.98 }
        }
        animate={{ x: 0, y: 0, opacity: 1, scale: 1 }}
        transition={{ duration: 0.22, ease }}
        className={
          align === 'right'
            ? size === 'xl'
              ? 'h-full w-full max-w-[1280px]'
              : size === 'lg'
              ? 'h-full w-full max-w-2xl'
              : 'h-full w-full max-w-md'
            : size === 'xl'
              ? 'h-[min(88dvh,860px)] min-w-0 w-full max-w-[1100px]'
              : size === 'lg'
                ? 'w-full max-w-3xl'
                : 'w-full max-w-lg'
        }
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </motion.div>
    </div>
  )
}
