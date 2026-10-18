import type { DecisionType, InstanceOut } from '../api/contracts'

/**
 * Why a case waiting on the manager goes first. The inbox sorts by what a person would
 * look at first: documents already past their due date, then those about to be, then the
 * big ones. The due date is the issue date plus the legal default term (Ley 3/2004).
 * Plain arithmetic on the list; the engine never sees any of it.
 */
export const PAYMENT_TERM_DAYS = 30
export const SOON_DAYS = 7
/** Share of the queue, by amount, that counts as "big". */
const LARGE_SHARE = 0.2

/** The invoice-payment pack's symbols this screen reads. */
const SYMBOL = { amount: 'total', date: 'date', party: 'issuer_name', number: 'invoice_number' }

export type Flag = 'overdue' | 'soon' | 'large' | 'reviewer' | 'unread'

export type Triage = {
  item: InstanceOut
  party: string | null
  number: string | null
  amount: number | null
  issued: Date | null
  due: Date | null
  /** Days from the reference date to the due date; negative when past. */
  daysLeft: number | null
  flags: Flag[]
  score: number
}

const DAY = 86_400_000

/** "2026-04-06" to a local date at midnight; null for an impossible one like 2026-02-31. */
export function parseDay(value: unknown): Date | null {
  if (typeof value !== 'string') return null
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})(?:$|T)/)
  if (!match) return null
  const [year, month, day] = match.slice(1).map(Number)
  const date = new Date(0)
  date.setFullYear(year, month - 1, day)
  date.setHours(0, 0, 0, 0)
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null
}

export function dayKey(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function amountOf(value: unknown): number | null {
  const number = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : NaN
  return Number.isFinite(number) ? number : null
}

/** Every case with its flags, most urgent first. `today` is the manager's reference day. */
export function triage(items: InstanceOut[], today: Date): Triage[] {
  const reference = startOfDay(today)
  const amounts = items
    .map((item) => amountOf(item.values?.[SYMBOL.amount]))
    .filter((value): value is number => value != null)
    .sort((a, b) => b - a)
  const largeFrom = amounts.length >= 5 ? amounts[Math.floor(amounts.length * LARGE_SHARE)] : Infinity

  return items
    .map((item) => {
      const values = item.values ?? {}
      const issued = parseDay(values[SYMBOL.date])
      // Payment terms count calendar days; adding 24-hour periods crosses midnight
      // incorrectly when daylight saving time ends.
      const due = issued ? new Date(issued) : null
      if (due) due.setDate(due.getDate() + PAYMENT_TERM_DAYS)
      const daysLeft = due ? Math.round((due.getTime() - reference.getTime()) / DAY) : null
      const amount = amountOf(values[SYMBOL.amount])

      const flags: Flag[] = []
      if (!item.values) flags.push('unread')
      if (daysLeft != null && daysLeft < 0) flags.push('overdue')
      else if (daysLeft != null && daysLeft <= SOON_DAYS) flags.push('soon')
      if (amount != null && amount >= largeFrom) flags.push('large')
      if (item.review_pending) flags.push('reviewer')

      // Overdue beats soon beats big; within a tier, the older or closer goes first.
      let score = 0
      if (flags.includes('overdue')) score += 1000 + Math.min(-(daysLeft ?? 0), 365)
      if (flags.includes('soon')) score += 500 + (SOON_DAYS - (daysLeft ?? 0))
      if (flags.includes('large')) score += 200
      if (flags.includes('reviewer')) score += 50
      if (flags.includes('unread')) score += 25

      return {
        item,
        party: text(values[SYMBOL.party]),
        number: text(values[SYMBOL.number]),
        amount,
        issued,
        due,
        daysLeft,
        flags,
        score,
      }
    })
    .sort((a, b) => b.score - a.score || (b.amount ?? 0) - (a.amount ?? 0) || a.item.id - b.item.id)
}

/** The flag as a manager reads it. */
export function flagLabel(flag: Flag, daysLeft: number | null): string {
  switch (flag) {
    case 'overdue': {
      const days = -(daysLeft ?? 0)
      return days === 0 ? 'Vence hoy' : `Vencida hace ${days} día${days === 1 ? '' : 's'}`
    }
    case 'soon':
      return daysLeft === 0
        ? 'Vence hoy'
        : `Vence en ${daysLeft} día${daysLeft === 1 ? '' : 's'}`
    case 'large':
      return 'Importe alto'
    case 'reviewer':
      return 'El revisor no está de acuerdo'
    case 'unread':
      return 'Sin leer'
  }
}

/** The tier a case sits in, for its colour. */
export function severity(flags: Flag[]): 'high' | 'medium' | 'low' {
  if (flags.includes('overdue')) return 'high'
  if (flags.includes('soon') || flags.includes('large')) return 'medium'
  return 'low'
}

/** Engine codes a manager should not have to decode. */
const CAUSES: Record<string, string> = {
  MISSING_DATA: 'Faltan datos en el documento',
  UNVERIFIED_DATA: 'Datos leídos sin confirmar',
  SCAN_REVIEW: 'Documento escaneado: revisar la lectura',
  RULE_ERROR: 'Una regla no pudo evaluarse',
  RULE_CONFLICT: 'Dos reglas no se ponen de acuerdo',
  SOURCE_UNAVAILABLE: 'Una fuente de datos no respondió',
}

/** A decision's reason in one plain sentence, and the raw detail after it. */
export function plainReason(
  item: Pick<InstanceOut, 'decision' | 'author' | 'reason'>,
  decisionTypes: Pick<DecisionType, 'name' | 'is_default'>[],
): { title: string; detail: string | null } {
  const reason = item.reason?.trim()
  if (!reason) {
    const automaticDefault =
      item.author === 'engine' &&
      decisionTypes.some((type) => type.name === item.decision && type.is_default)
    return {
      title: automaticDefault
        ? 'Todas las comprobaciones del proceso han pasado'
        : 'Decisión registrada sin motivo',
      detail: null,
    }
  }
  const match = reason.match(/^([A-Z_]+):\s*(.*)$/s)
  if (match && CAUSES[match[1]]) return { title: CAUSES[match[1]], detail: match[2] || null }
  return { title: reason, detail: null }
}

export function formatAmount(value: number | null): string {
  if (value == null) return '—'
  return value.toLocaleString('es-ES', { style: 'currency', currency: 'EUR' })
}

export function formatDay(date: Date | null): string {
  if (!date) return '—'
  return date.toLocaleDateString('es-ES', { day: 'numeric', month: 'short', year: 'numeric' })
}
