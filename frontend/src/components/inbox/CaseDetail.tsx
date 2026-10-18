import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { AnimatePresence, motion } from 'motion/react'
import { hashKey, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, ChevronDown, FileText, X } from 'lucide-react'
import { api } from '../../api/client'
import { families, keys } from '../../api/queries'
import type {
  DecisionProposalPayload,
  InstanceDetail,
  ProcessDetail,
  RuleResult,
} from '../../api/contracts'
import { cn } from '../../lib/cn'
import { formatRunDate, humanize } from '../../lib/format'
import { paths } from '../../lib/paths'
import { ruleLabel } from '../../lib/process'
import { t } from '../../i18n'
import {
  formatAmount,
  formatDay,
  plainReason,
  type Triage,
} from '../../lib/urgency'
import { DocumentPane } from '../run/DocumentPane'
import { DocumentPopup } from '../run/DocumentPopup'
import { SenderContact } from '../run/SenderContact'
import { Button, Textarea } from '../shell/Controls'
import { ErrorNotice } from '../shell/Notice'
import { Overlay } from '../shell/Overlay'
import { StatusBadge } from '../shell/StatusBadge'
import { TerminalLoader } from '../shell/TerminalLoader'

const ease = [0.23, 1, 0.32, 1] as const

/**
 * One case, as a manager reads it: who, how much, why it is here and what to do. The
 * decision and its reason go to the history, where the assistant learns from them.
 * Rule results, hashes and the trace stay behind "Detalles técnicos".
 */
export function CaseDetail({
  process,
  entry,
  onClose,
  onResolved,
}: {
  process: ProcessDetail
  entry: Triage
  onClose: () => void
  onResolved: (id: number) => void
}) {
  const { item } = entry
  const [showDocument, setShowDocument] = useState(false)
  const [technical, setTechnical] = useState(false)

  const instance = useQuery({
    queryKey: keys.instance(item.id),
    queryFn: () => api.getInstance(item.id),
  })
  const waiting = process.decision_types.some(
    (type) => type.name === item.decision && type.requires_human,
  ) || item.review_pending
  const reason = plainReason(item, process.decision_types)
  const results = (instance.data?.decisions.at(-1)?.results ?? []) as RuleResult[]
  const fired = results.filter((result) => result.fires === true)
  const review = instance.data?.reviews.at(-1)

  return (
    <div className="flex h-full min-h-0 overflow-hidden rounded-[20px] bg-surface shadow-pop ring-1 ring-line">
      {/* The document itself, open beside the decision: reviewing is reading. */}
      <div className="hidden min-h-0 min-w-0 flex-1 flex-col border-r border-hairline bg-canvas md:flex">
        <DocumentPane instanceId={item.id} name={item.name} embedded />
      </div>
      <div className="flex min-h-0 w-full flex-col md:w-[440px] md:shrink-0">
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-hairline px-5 py-4">
        <div className="min-w-0">
          {entry.party ? (
            <p className="truncate font-mono text-[11px] text-faint">{item.name}</p>
          ) : null}
          <h2 className="mt-0.5 truncate text-[20px] font-medium tracking-[-0.03em]">
            {entry.party ?? item.name}
          </h2>
          <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted">
            <span className="font-mono text-[15px] tabular-nums text-ink">
              {formatAmount(entry.amount)}
            </span>
            <span>Emitida {formatDay(entry.issued)}</span>
            <span>Vence {formatDay(entry.due)}</span>
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Cerrar"
          className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-muted hover:bg-canvas hover:text-ink"
        >
          <X size={15} strokeWidth={1.75} />
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
        <section>
          {waiting ? null : <p className="text-[11px] text-faint">Decisión</p>}
          {waiting ? (
            <>
              <p className="text-[15px] tracking-[-0.01em] text-ink">{reason.title}</p>
              {fired.length ? (
                <FiredRules processId={process.id} fired={fired} />
              ) : reason.detail ? (
                <p className="mt-0.5 text-[12.5px] text-muted">{reason.detail}</p>
              ) : null}
              {item.review_pending && review ? (
                <p className="mt-2 text-[12.5px] text-muted">
                  El revisor recomienda {review.recommendation ?? '—'}
                  {review.reasoning ? `: ${review.reasoning}` : ''}
                </p>
              ) : null}
            </>
          ) : (
            <div className="mt-1 space-y-1 text-[13px]">
              <p className="flex items-center gap-2">
                {item.decision ? (
                  <StatusBadge value={item.decision} decisionTypes={process.decision_types} />
                ) : null}
                <span className="text-muted">
                  {item.author === 'engine' ? 'por el proceso' : `por ${item.author ?? '—'}`}
                  {item.decided_at ? ` · ${formatRunDate(item.decided_at)}` : ''}
                </span>
              </p>
              <p className="text-ink">{reason.title}</p>
              {reason.detail ? <p className="text-[12.5px] text-muted">{reason.detail}</p> : null}
            </div>
          )}
        </section>

        <Button
          tone="soft"
          className="md:hidden"
          onClick={() => setShowDocument(true)}
          disabled={!instance.data}
        >
          <FileText size={12} strokeWidth={1.75} />
          Ver el documento
        </Button>

        {waiting ? (
          <Decide
            process={process}
            instanceId={item.id}
            onResolved={() => onResolved(item.id)}
          />
        ) : null}

        <section className="border-t border-hairline pt-3">
          <button
            type="button"
            aria-expanded={technical}
            onClick={() => setTechnical((open) => !open)}
            className="flex w-full items-center justify-between text-left text-[12.5px] text-muted hover:text-ink"
          >
            Detalles técnicos
            <ChevronDown
              size={14}
              strokeWidth={1.6}
              className={cn('transition-transform', technical && 'rotate-180')}
            />
          </button>
          {technical ? (
            <Technical processId={process.id} instance={instance.data} error={instance.error} />
          ) : null}
        </section>
        {instance.data ? <SenderContact instance={instance.data} /> : null}
      </div>

      </div>
      {showDocument && instance.data ? (
        <DocumentPopup instance={instance.data} trace={undefined} onClose={() => setShowDocument(false)} />
      ) : null}
    </div>
  )
}

/**
 * The rules that sent the case here, folded: each line is why it fired; hovering it shows
 * the rule itself. The instance's results carry only ids, so the rules come from the list.
 */
function FiredRules({ processId, fired }: { processId: number; fired: RuleResult[] }) {
  const [open, setOpen] = useState(false)
  const rules = useQuery({
    queryKey: keys.rules(processId),
    queryFn: () => api.listRules(processId),
  })
  const byId = new Map((rules.data ?? []).map((rule) => [rule.id, rule]))

  return (
    <div className="mt-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-1.5 text-[12.5px] text-muted hover:text-ink"
      >
        {fired.length === 1 ? '1 regla' : `${fired.length} reglas`}
        <ChevronDown
          size={14}
          strokeWidth={1.6}
          className={cn('transition-transform', open && 'rotate-180')}
        />
      </button>
      {open ? (
        <ul className="mt-1.5 space-y-1 text-[12.5px]">
          {fired.map((result) => {
            const rule = byId.get(result.rule_id)
            return (
              <li key={result.rule_id} className="group relative">
                <Link
                  to={paths.rule(processId, result.rule_id)}
                  className="flex gap-2 rounded-[8px] px-1.5 py-1 text-muted hover:bg-canvas hover:text-ink"
                >
                  <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-faint" />
                  <span className="min-w-0 break-words">
                    {result.reason || (rule ? ruleLabel(rule) : `Regla ${result.rule_id}`)}
                  </span>
                </Link>
                <div
                  role="tooltip"
                  className="pointer-events-none absolute left-0 right-0 top-full z-20 mt-1 hidden rounded-[12px] bg-surface p-3 text-[12px] shadow-pop ring-1 ring-line group-hover:block"
                >
                  <p className="flex items-center justify-between gap-2 font-mono text-[10.5px] text-faint">
                    <span>Regla {result.rule_id}</span>
                    {rule ? <span>{humanize(rule.decision)}</span> : null}
                  </p>
                  {rule ? (
                    <>
                      <p className="mt-1 text-ink">{ruleLabel(rule)}</p>
                      {rule.summary ? (
                        <p className="mt-1 leading-5 text-muted">{rule.text}</p>
                      ) : null}
                    </>
                  ) : (
                    <p className="mt-1 text-muted">{result.rule_summary || result.rule_text || '—'}</p>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}

/**
 * The manager's call. The options are the outcomes the process can close a case with;
 * an optional reason adds context to the history. A suggestion is
 * asked for, never fetched on its own: it is a model call.
 */
function Decide({
  process,
  instanceId,
  onResolved,
}: {
  process: ProcessDetail
  instanceId: number
  onResolved: () => void
}) {
  const queryClient = useQueryClient()
  const options = process.decision_types.filter((type) => !type.requires_human)
  const [decision, setDecision] = useState<string | null>(null)
  const [reason, setReason] = useState('')

  // An escalation proposal already open for the case; a new one only when asked for.
  const open = useQuery({
    queryKey: keys.caseProposal(instanceId),
    queryFn: async () => {
      const proposals = await api.listProposals(process.id, 'open')
      return (
        proposals.find((item) => item.instance_id === instanceId && item.channel === 'escalation') ??
        null
      )
    },
    retry: false,
  })
  const ask = useMutation({ mutationFn: () => api.proposeDecision(instanceId) })
  const proposal = ask.data ?? open.data ?? undefined
  const payload = proposal?.payload as DecisionProposalPayload | undefined

  const invalidate = () => {
    const settled = hashKey(keys.caseProposal(instanceId))
    for (const name of [...families.decisions, ...families.proposals]) {
      void queryClient.invalidateQueries({
        queryKey: [name],
        predicate: (query) => query.queryHash !== settled,
      })
    }
  }

  const resolve = useMutation({
    mutationFn: async (chosen: string) => {
      const text = reason.trim()
      if (proposal?.status === 'open' && payload?.proposed === chosen) {
        return api.acceptProposal(proposal.id, text || undefined)
      }
      return api.resolve(instanceId, {
        decision: chosen,
        reason: text,
        proposal_id: proposal?.status === 'open' ? proposal.id : null,
      })
    },
    onSuccess: () => {
      invalidate()
      window.setTimeout(onResolved, 900)
    },
  })

  /**
   * Two steps, no separate confirm button: the first click arms an option, the second
   * decides. An armed option disarms itself after a few seconds or on Escape.
   */
  const [armed, setArmed] = useState<string | null>(null)
  const choose = (name: string) => {
    if (armed !== name) return setArmed(name)
    setArmed(null)
    setDecision(name)
    resolve.mutate(name)
  }
  useEffect(() => {
    if (!armed) return
    const timer = window.setTimeout(() => setArmed(null), 4000)
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setArmed(null)
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('keydown', onKey)
    }
  }, [armed])

  const [suggesting, setSuggesting] = useState(false)
  /** Accepting the proposal resolves the case with its decision, right from the popup. */
  const apply = useMutation({
    mutationFn: () => api.acceptProposal(proposal!.id),
    onSuccess: () => {
      setDecision(payload?.proposed ?? null)
      setSuggesting(false)
      invalidate()
      window.setTimeout(onResolved, 900)
    },
  })
  const ready = !resolve.isPending && !apply.isPending
  const openSuggestion = () => {
    setSuggesting(true)
    if (!proposal && !ask.isPending) ask.mutate()
  }

  return (
    <section className="rounded-[14px] px-4 py-3.5 ring-1 ring-line">
      <AnimatePresence mode="wait" initial={false}>
        {resolve.isSuccess || apply.isSuccess ? (
          <motion.div
            key="done"
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.3, ease }}
            className="flex items-center gap-3 py-3"
          >
            <CheckCircle2 size={22} strokeWidth={1.5} className="text-pagar" />
            <div>
              <p className="text-[14px] font-medium">Decidida: {decision?.replaceAll('_', ' ')}</p>
              <p className="text-[12.5px] text-muted">
                Queda en el historial. El asistente lo tendrá en cuenta en los casos parecidos.
              </p>
            </div>
          </motion.div>
        ) : (
          <motion.div key="form" exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
            <div className="flex items-center justify-between gap-3">
              <p className="text-[14px] font-medium">¿Qué hacemos con ella?</p>
              <button
                type="button"
                onClick={openSuggestion}
                className="rounded-full px-3 py-1 text-[12px] text-muted ring-1 ring-line hover:bg-canvas hover:text-ink"
              >
                Ver sugerencia
              </button>
            </div>

            <label className="mt-3 block">
              <span className="text-[12px] text-muted">{t('reviewUi.optionalReason')}</span>
              <Textarea
                rows={3}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Ej.: el proveedor confirmó por teléfono que el pedido es correcto."
                className="mt-1"
              />
              <span className="mt-1.5 block text-[11px] text-muted">{t('reviewUi.reasonHint')}</span>
            </label>

            {resolve.isError ? (
              <div className="mt-3">
                <ErrorNotice error={resolve.error} />
              </div>
            ) : null}

            <div
              className="mt-3 grid gap-2"
              style={{ gridTemplateColumns: `repeat(${Math.max(options.length, 1)}, minmax(0, 1fr))` }}
            >
              {options.map((option) => {
                const isArmed = armed === option.name
                const saving = resolve.isPending && decision === option.name
                return (
                  <motion.button
                    key={option.name}
                    type="button"
                    whileTap={ready ? { scale: 0.97 } : undefined}
                    disabled={!ready}
                    onClick={() => choose(option.name)}
                    className={cn(
                      'flex min-h-[64px] flex-col items-center justify-center rounded-[14px] px-3 py-2.5 ring-1 transition-[background-color,color,opacity,box-shadow] duration-200',
                      'disabled:cursor-not-allowed disabled:opacity-45',
                      option.is_default
                        ? isArmed || saving
                          ? 'bg-pagar text-white ring-pagar'
                          : 'bg-pagar-soft text-pagar ring-pagar/25 hover:ring-pagar'
                        : isArmed || saving
                          ? 'bg-nopagar text-white ring-nopagar'
                          : 'bg-nopagar-soft text-nopagar ring-nopagar/25 hover:ring-nopagar',
                      armed && !isArmed && 'opacity-50',
                    )}
                  >
                    <span className="text-[16px] font-medium tracking-[-0.01em]">
                      {saving ? 'Guardando…' : humanize(option.name)}
                    </span>
                    <AnimatePresence initial={false}>
                      {isArmed && !saving ? (
                        <motion.span
                          initial={{ opacity: 0, height: 0 }}
                          animate={{ opacity: 0.9, height: 'auto' }}
                          exit={{ opacity: 0, height: 0 }}
                          transition={{ duration: 0.18, ease }}
                          className="text-[11.5px]"
                        >
                          Pulsa otra vez para confirmar
                        </motion.span>
                      ) : null}
                    </AnimatePresence>
                  </motion.button>
                )
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {suggesting ? (
        <Overlay onClose={() => setSuggesting(false)}>
          <SuggestionDialog
            loading={ask.isPending}
            error={ask.error ?? apply.error}
            payload={payload}
            rationale={proposal?.rationale}
            applying={apply.isPending}
            onApply={() => apply.mutate()}
            onClose={() => setSuggesting(false)}
          />
        </Overlay>
      ) : null}
    </section>
  )
}

/**
 * What the assistant would do, in a popup: the decision, a few short reasons, the full
 * reasoning only on demand, and one button that applies it.
 */
function SuggestionDialog({
  loading,
  error,
  payload,
  rationale,
  applying,
  onApply,
  onClose,
}: {
  loading: boolean
  error: unknown
  payload: DecisionProposalPayload | undefined
  rationale: string | undefined
  applying: boolean
  onApply: () => void
  onClose: () => void
}) {
  const [full, setFull] = useState(false)
  const why = (payload?.why ?? []).slice(0, 3)

  return (
    <div className="rounded-[20px] bg-surface p-5 shadow-pop ring-1 ring-line">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[12px] text-muted">
          Sugerencia del asistente
        </p>
        <button
          type="button"
          onClick={onClose}
          aria-label="Cerrar"
          className="-mr-1 -mt-1 grid h-7 w-7 place-items-center rounded-full text-muted hover:bg-canvas hover:text-ink"
        >
          <X size={14} strokeWidth={1.75} />
        </button>
      </div>

      {loading ? (
        <TerminalLoader
          className="mt-4"
          verbs={['leyendo el caso', 'mirando casos parecidos', 'redactando la sugerencia']}
        />
      ) : error ? (
        <div className="mt-4">
          <ErrorNotice error={error} />
        </div>
      ) : payload ? (
        <>
          <p className="mt-3 text-[22px] font-medium tracking-[-0.03em]">{humanize(payload.proposed)}</p>
          {why.length ? (
            <ul className="mt-3 space-y-1.5 text-[13px] text-muted">
              {why.map((line) => (
                <li key={line} className="flex gap-2">
                  <span className="mt-[8px] h-1 w-1 shrink-0 rounded-full bg-faint" />
                  <span className="line-clamp-2">{line}</span>
                </li>
              ))}
            </ul>
          ) : null}
          {rationale ? (
            <div className="mt-3">
              <button
                type="button"
                onClick={() => setFull((open) => !open)}
                className="text-[12px] text-faint hover:text-ink"
              >
                {full ? 'Ocultar razonamiento' : 'Ver razonamiento completo'}
              </button>
              {full ? (
                <p className="mt-2 max-h-48 overflow-y-auto rounded-[10px] bg-canvas px-3 py-2 text-[12px] leading-5 text-muted">
                  {rationale}
                </p>
              ) : null}
            </div>
          ) : null}
          <div className="mt-5 flex justify-end gap-2">
            <Button tone="ghost" onClick={onClose}>
              Decido yo
            </Button>
            <Button tone="primary" disabled={applying} onClick={onApply}>
              {applying ? 'Aplicando…' : `Aplicar: ${humanize(payload.proposed)}`}
            </Button>
          </div>
        </>
      ) : null}
    </div>
  )
}

/** Rule by rule, the raw reason, and where to dig further. */
function Technical({
  processId,
  instance,
  error,
}: {
  processId: number
  instance: InstanceDetail | undefined
  error: unknown
}) {
  if (error) return <div className="mt-3"><ErrorNotice error={error} /></div>
  if (!instance) return null
  const last = instance.decisions.at(-1)
  const results = (last?.results ?? []) as RuleResult[]

  return (
    <div className="mt-3 space-y-3 text-[12px]">
      <dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono text-[11.5px]">
        <dt className="text-faint">estado</dt>
        <dd>{t(`instanceStatus.${instance.status}`)}</dd>
        <dt className="text-faint">motivo</dt>
        <dd className="break-words">{instance.reason ?? '—'}</dd>
        <dt className="text-faint">autor</dt>
        <dd>{last?.author ?? '—'}</dd>
        <dt className="text-faint">rules_hash</dt>
        <dd>{last?.rules_hash.slice(0, 12) ?? '—'}</dd>
        <dt className="text-faint">decisiones</dt>
        <dd>{instance.decisions.length}</dd>
      </dl>
      {results.length ? (
        <ul className="divide-y divide-hairline overflow-hidden rounded-[10px] ring-1 ring-line">
          {results.map((result) => (
            <li key={result.rule_id} className="flex items-start gap-2 px-3 py-2">
              <span
                className={cn(
                  'mt-0.5 shrink-0 rounded-full px-1.5 font-mono text-[10px]',
                  result.fires === true
                    ? 'bg-canvas text-ink'
                    : result.fires === false
                      ? 'bg-pagar-soft text-pagar'
                      : 'bg-ocr-soft text-ocr',
                )}
              >
                {result.fires === true ? 'salta' : result.fires === false ? 'no' : 'error'}
              </span>
              <span className="min-w-0">
                <span className="block text-ink">
                  {result.rule_summary || result.rule_text || `Regla ${result.rule_id}`}
                </span>
                <span className="block font-mono text-[11px] text-muted">{result.reason}</span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="flex flex-wrap gap-3">
        <Link to={paths.instance(processId, instance.id)} className="text-ink underline">
          Traza completa
        </Link>
        <Link to={`${paths.review(processId)}?i=${instance.id}`} className="text-ink underline">
          Abrir en Revisión
        </Link>
      </div>
    </div>
  )
}
