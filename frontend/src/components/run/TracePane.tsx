import { useId, useState, type ReactNode } from 'react'
import { Activity, CornerDownRight, FileUp, Play, Circle, CheckCircle2, ChevronDown, ClipboardList, Database, FileCode2, FileSearch, History, ListChecks, ListTree, XCircle, type LucideIcon } from 'lucide-react'
import type {
  DecisionType,
  InstanceDetail,
  InstanceTrace,
  SpanNode,
  SymbolIO,
  SymbolReading,
  VersionOut,
} from '../../api/contracts'
import { Link } from 'react-router'
import { formatMs, formatRunDate, humanize } from '../../lib/format'
import { plainReason } from '../../lib/urgency'
import { ALERTS_TAB, paths } from '../../lib/paths'
import { cn } from '../../lib/cn'
import { t } from '../../i18n'
import { label } from '../../lib/status'
import { JsonHighlight } from '../../lib/jsonHighlight'
import { symbolLabel } from '../../lib/symbols'
import { ExpandableText } from '../shell/ExpandableText'
import { EmptyState } from '../shell/Notice'
import { StatusBadge } from '../shell/StatusBadge'
import { DocumentPopup } from './DocumentPopup'
import { SenderContact } from './SenderContact'

/**
 * Why this instance ended where it did: the decision, the result of every rule
 * the engine ran, the symbols with their origin, and the trace of every step.
 */
export function TracePane({
  instance,
  trace,
  schema,
  versions,
  decisionTypes,
}: {
  instance: InstanceDetail | undefined
  trace: InstanceTrace | undefined
  /** The process's symbols: which are required, and which hold a whole transcript. */
  schema?: SymbolIO[]
  versions?: VersionOut[]
  decisionTypes?: DecisionType[]
}) {
  const [document, setDocument] = useState<{ instanceId: number; symbol?: string } | null>(null)
  if (!instance) {
    return (
      <aside className="flex min-h-[240px] min-w-0 flex-1 flex-col items-center justify-center lg:h-full lg:min-h-0">
        <EmptyState icon={FileSearch} title="Elige un documento">
          Verás su decisión, las reglas que saltaron y la evidencia de cada dato.
        </EmptyState>
      </aside>
    )
  }

  const current = label(instance)
  const shown =
    current === instance.status
      ? t(`instanceStatus.${instance.status}`)
      : current.replaceAll('_', ' ')
  const latest = instance.decisions.at(-1)
  const mailOrigin = instance.events.find(event => event.data?.automation === 'mail_ingestion')
    ?.data?.mail_origin as Record<string, unknown> | undefined
  // The trace carries each result with its rule's text.
  const results = trace?.decisions.at(-1)?.rule_results ?? []
  const symbols = bySignificance(
    Object.entries(instance.symbols ?? {}) as [string, SymbolReading][],
    schema ?? [],
  )
  const fired = results.filter((result) => result.fires === true).length
  const errors = results.filter((result) => result.fires === null).length
  const DecisionIcon =
    current === 'PAGAR' || current === 'APROBAR'
      ? CheckCircle2
      : current === 'NO_PAGAR' || current === 'RECHAZAR'
        ? XCircle
        : ClipboardList

  return (
    <aside className="flex min-h-0 min-w-0 flex-1 flex-col px-4 pb-4 sm:px-6 lg:h-full lg:overflow-y-auto">
      <DocumentHeader instance={instance} onOpen={() => setDocument({ instanceId: instance.id })} />
      {mailOrigin && <details className="mb-4 rounded-lg border border-hairline p-3 text-sm">
        <summary>Recibido por correo · {String(mailOrigin.original_name ?? instance.name)}</summary>
        <p className="mt-2 break-all">{String(mailOrigin.sender ?? '')} · {String(mailOrigin.subject ?? '')}</p>
        <p className="mt-1 break-all text-xs text-muted">{String(mailOrigin.account)} · {String(mailOrigin.folder)} · UID {String(mailOrigin.uid)} · Parte {String(mailOrigin.part)}</p>
      </details>}
      {document?.instanceId === instance.id ? (
        <DocumentPopup instance={instance} trace={trace} initialSymbol={document.symbol} onClose={() => setDocument(null)} />
      ) : null}

      <div className="mx-auto w-full max-w-[820px] rounded-[16px] bg-surface px-4 py-6 ring-1 ring-line sm:px-6">
        <div className="flex items-start gap-4">
          <span
            className={cn(
              'grid h-10 w-10 shrink-0 place-items-center rounded-[12px]',
              (current === 'PAGAR' || current === 'APROBAR') && 'bg-pagar-soft text-pagar',
              (current === 'NO_PAGAR' || current === 'RECHAZAR') && 'bg-nopagar-soft text-nopagar',
              (current === 'ESCALAR' || current === 'PENDING') && 'bg-canvas text-ink',
            )}
          >
            <DecisionIcon size={19} strokeWidth={1.8} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-[11px] text-muted">Decisión final</p>
            <p
              className={cn(
                'mt-1 text-[26px] font-medium leading-none tracking-[-0.045em]',
                (current === 'PAGAR' || current === 'APROBAR') && 'text-pagar',
                (current === 'NO_PAGAR' || current === 'RECHAZAR') && 'text-nopagar',
                (current === 'ESCALAR' || current === 'PENDING') && 'text-ink',
              )}
            >
              {shown}
            </p>
            <p className="mt-2 text-[12.5px] leading-5 text-muted">
              {latest
                ? latest.reason?.trim() || plainReason({ decision: instance.decision, author: latest.author, reason: latest.reason }, decisionTypes ?? []).title
                : 'El proceso todavía no ha emitido una decisión.'}
            </p>
          </div>
        </div>

        {latest ? (
          <div className="mt-5 grid grid-cols-3 gap-px overflow-hidden rounded-[10px] bg-rule ring-1 ring-line">
            <DecisionStat label="Reglas evaluadas" value={results.length} />
            <DecisionStat label="Activadas" value={fired} />
            <DecisionStat label="Errores" value={errors} />
          </div>
        ) : null}
        <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
          <span>{latest?.author === 'engine' ? 'Decidido por el motor' : latest?.author}</span>
          {latest ? <span className="font-mono text-faint">{latest.rules_hash.slice(0, 12)}</span> : null}
          {/* traceability-gaps (fix/traceability-gaps): which published version decided, who published it and when.
              If merging a newer version from Carlos, keep his UI and preserve: trace.version shown as vN · author · date. */}
          {trace?.version ? (
            <span>
              {t('trace.version')} v{trace.version.number} · {trace.version.author} ·{' '}
              {formatRunDate(trace.version.created_at)}
            </span>
          ) : null}
        </div>
      </div>

      <div className="mx-auto mt-6 w-full max-w-[820px]">
        <div className="mb-3">
          <h3 className="text-[14px] font-medium">{t('trace.technicalDetails')}</h3>
          <p className="mt-1 text-[12px] text-muted">{t('trace.technicalHint')}</p>
        </div>
        <div className="overflow-hidden rounded-xl border border-hairline bg-surface">
        {results.length ? (
        <Block title={t('trace.rules')} count={results.length} icon={ListChecks}>
          <ul className="divide-y divide-hairline">
            {results.map((outcome) => (
              <li key={outcome.rule_id} className="px-4 py-3">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-[12px] text-ink">{outcome.rule_summary || outcome.rule_text || `Regla ${outcome.rule_id}`}</span>
                  <span
                    className={cn(
                      'shrink-0 rounded-md px-2 py-1 text-[10px]',
                      outcome.fires === null && 'bg-nopagar-soft text-nopagar',
                      outcome.fires === true && 'bg-well text-ink',
                      outcome.fires === false && 'text-faint',
                    )}
                  >
                    {t(outcome.fires === null ? 'trace.error' : outcome.fires ? 'trace.fired' : 'trace.notFired')}
                  </span>
                </div>
                {outcome.fires !== false ? (
                  <p
                    className={cn(
                      'mt-1 break-words font-mono text-[11px] leading-5',
                      outcome.fires === null ? 'text-nopagar' : 'text-muted',
                    )}
                  >
                    {outcome.reason}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        </Block>
        ) : null}

        <Block title={t('trace.symbols')} count={symbols.length} icon={ListTree}>
        {symbols.length === 0 ? (
          <p className="px-3 py-3 text-[12px] text-muted">
            Nadie ha extraído los símbolos de esta instancia todavía.
          </p>
        ) : (
          <ul className="divide-y divide-hairline">
            {symbols.map(([name, symbol]) => {
              const structured = structuredValue(symbol.value)
              const value = symbol.value == null ? null : String(symbol.value)
              const long = structured !== null || (value != null && value.length > LONG_VALUE)
              return (
              <li key={name} className="px-4 py-3">
                <div className="flex items-baseline justify-between gap-3">
                  {symbol.origin?.match(/^(document|scan):/) ? (
                    <button
                      type="button"
                      aria-label={`View ${name} in original document`}
                      onClick={() => setDocument({ instanceId: instance.id, symbol: name })}
                      className={cn(
                        'inline-flex items-center gap-1.5 text-left text-[12px] text-ocr underline decoration-ocr/30 underline-offset-4 hover:decoration-ocr',
                        long ? 'min-w-0' : 'max-w-[55%] shrink-0',
                      )}
                    >
                      <FileSearch size={12} className="shrink-0" />
                      <span className="break-words">{symbolLabel(name)}</span>
                    </button>
                  ) : (
                    <span
                      className={cn(
                        'break-words text-[12px] text-muted',
                        long ? 'min-w-0' : 'max-w-[55%] shrink-0',
                      )}
                    >
                      {symbolLabel(name)}
                    </span>
                  )}
                  {long ? null : (
                    <span
                      title={value ?? undefined}
                      className={cn(
                        'min-w-0 truncate text-right font-mono text-[12px]',
                        value == null ? 'text-faint' : 'text-ink',
                      )}
                    >
                      {value ?? '—'}
                    </span>
                  )}
                </div>
                {structured !== null ? (
                  <JsonHighlight value={structured} className="mt-2 max-h-72 rounded-lg" />
                ) : long ? (
                  <ExpandableText
                    text={readable(value ?? '')}
                    className="mt-1 rounded-[8px] bg-canvas px-2.5 py-1.5 font-mono text-[11px] leading-5 text-ink"
                  />
                ) : null}
                {symbol.origin ? (
                  <p className="truncate text-[10.5px] text-faint" title={symbol.origin}>
                    {symbol.origin}
                  </p>
                ) : null}
              </li>
              )
            })}
          </ul>
        )}
        </Block>

        {/* traceability-gaps (fix/traceability-gaps): the sources of truth this decision read, and what
            still waits on this case. If merging a newer version from Carlos, keep his UI and preserve:
            trace.sources_read rows and trace.pending links to Revisión with ?i=<instance>. */}
        {trace?.sources_read.length ? (
          <Block title={t('trace.sourcesRead')} count={trace.sources_read.length} icon={Database}>
            <ul className="divide-y divide-hairline">
              {trace.sources_read.map((source) => (
                <li key={source.source} className="px-4 py-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="min-w-0 flex-1 break-words text-[12px] font-medium text-ink">{source.source}</span>
                    <span className={cn('rounded-md px-2 py-0.5 text-[10px]', source.status === 'ok' ? 'bg-canvas text-muted' : 'bg-nopagar-soft text-nopagar')}>
                      {t(source.status === 'ok' ? 'trace.sourceOk' : 'trace.sourceError')}
                    </span>
                    <span className="text-[11px] tabular-nums text-muted">
                      {source.duration_ms == null ? '—' : formatMs(source.duration_ms)}
                    </span>
                  </div>
                  <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                    {[
                      ['requests', source.requests], ['retries', source.retries],
                      ['rateLimited', source.rate_limited], ['timeouts', source.timeouts],
                    ].map(([key, value]) => (
                      <div key={key}>
                        <dt className="text-[10.5px] text-muted">{t(`trace.${key}`)}</dt>
                        <dd className="mt-0.5 text-[13px] tabular-nums text-ink">{value}</dd>
                      </div>
                    ))}
                  </dl>
                </li>
              ))}
            </ul>
          </Block>
        ) : null}

        {trace ? <PendingBlock trace={trace} /> : null}

        <Block title={t('trace.steps')} count={trace?.spans.length ?? 0} icon={Activity}>
          {!trace?.spans.length ? <p className="px-4 py-3 text-[12px] text-muted">{t('trace.noSteps')}</p> : null}
          {(trace?.spans ?? []).map((span) => (
            <SpanBlock key={span.span_id} span={span} />
          ))}
        </Block>

        {instance.decisions.length > 1 ? (
        <Block
          title={t('trace.history')}
          count={instance.decisions.length}
          icon={History}
          openByDefault
        >
          <ul className="divide-y divide-hairline">
            {instance.decisions.map((decision) => {
              const version = versions?.find((item) => item.id === decision.version_id)
              return (
                <li key={decision.id} className="flex items-baseline gap-2 px-3 py-2">
                  <StatusBadge value={decision.decision} />
                  <span className="min-w-0 flex-1 truncate text-[11.5px] text-muted">
                    {decision.reason || 'Sin incidencias'}
                  </span>
                  <span className="shrink-0 font-mono text-[10.5px] text-faint">
                    {version ? `v${version.number}` : 'versión desconocida'} · {formatRunDate(decision.created_at)}
                  </span>
                </li>
              )
            })}
          </ul>
        </Block>
        ) : null}

        <Block title={t('trace.export')} icon={FileCode2}>
          <div className="px-4 py-3">
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-3 text-[12px]">
              <dt className="text-muted">{t('trace.file')}</dt>
              <dd className="break-all text-ink">{instance.name}</dd>
              <dt className="text-muted">{t('trace.result')}</dt>
              <dd>{trace?.exported_decision ? <StatusBadge value={trace.exported_decision} /> : <span className="text-muted">{t('trace.noExport')}</span>}</dd>
            </dl>
            <details className="group mt-4 border-t border-hairline pt-3">
              <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[11.5px] text-muted hover:text-ink [&::-webkit-details-marker]:hidden">
                <ChevronDown size={12} className="group-open:rotate-180" />
                {t('trace.viewJson')}
              </summary>
              <JsonHighlight className="mt-2 max-h-60 rounded-lg" value={{ file_id: instance.name, result: trace?.exported_decision ?? null }} />
            </details>
          </div>
        </Block>
        </div>
        <div className="mt-5"><SenderContact instance={instance} /></div>
      </div>
    </aside>
  )
}

function DocumentHeader({ instance, onOpen }: { instance: InstanceDetail; onOpen: () => void }) {
  return (
    <div className="mx-auto flex w-full max-w-[820px] items-center justify-between gap-3 py-3">
      <div className="min-w-0">
        <p className="text-[11px] text-muted">Resultado del proceso</p>
        <h2 className="mt-0.5 truncate font-mono text-[13px]">{instance.name}</h2>
      </div>
      <button
        type="button"
        onClick={onOpen}
        className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full bg-ink px-3 text-[12px] font-medium text-on-ink hover:bg-ink/90"
      >
        <FileSearch size={13} strokeWidth={1.7} />
        Abrir documento
      </button>
    </div>
  )
}

/**
 * traceability-gaps (fix/traceability-gaps): what this case still waits for, each linked to Revisión.
 * If merging a newer version from Carlos, keep his UI and preserve: waiting, open proposals and open
 * alerts from trace.pending, each opening Revisión on this instance (?i=).
 */
function PendingBlock({ trace }: { trace: InstanceTrace }) {
  const { pending } = trace
  const review = `${paths.review(trace.process_id)}?i=${trace.id}`
  const alerts = `${paths.review(trace.process_id)}?tipo=${ALERTS_TAB}&i=${trace.id}`
  const rows = [
    ...(pending.waiting_for_person
      ? [{ id: 'person', label: t(pending.review_pending ? 'trace.reviewPending' : 'trace.waitingForPerson'), to: review, date: undefined }]
      : []),
    ...pending.proposals.map((item) => ({
      id: `p-${item.id}`,
      label: `${t('trace.openProposals')} · ${t(`proposalKind.${item.kind}`)} #${item.id}`,
      to: review,
      date: item.created_at,
    })),
    ...pending.alerts.map((item) => ({
      id: `a-${item.id}`,
      label: `${t('trace.openAlerts')} · ${t(`alertTrigger.${item.kind}`)} #${item.id}`,
      to: alerts,
      date: item.created_at,
    })),
  ]
  return (
    <Block title={t('trace.pending')} count={rows.length} icon={ClipboardList} openByDefault={rows.length > 0}>
      {rows.length === 0 ? (
        <p className="px-3 py-3 text-[12px] text-muted">{t('trace.nothingPending')}</p>
      ) : (
        <ul className="divide-y divide-hairline">
          {rows.map((row) => (
            <li key={row.id} className="flex items-baseline gap-2 px-3 py-2">
              <Link to={row.to} className="min-w-0 flex-1 truncate text-[12px] text-ink underline">
                {row.label}
              </Link>
              {row.date ? <span className="shrink-0 text-[10.5px] text-faint">{formatRunDate(row.date)}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </Block>
  )
}

/** One span and, one level down each, the spans it started. */
function SpanBlock({ span }: { span: SpanNode }) {
  const duration = span.duration_ms == null ? '—' : formatMs(span.duration_ms)
  const Icon = span.status === 'error' ? XCircle
    : span.step.includes('upload') ? FileUp
    : span.step.includes('run') ? Play
    : span.step.includes('rule') ? ListChecks
    : span.step.includes('source') ? Database
    : span.step.includes('extract') ? FileSearch
    : CornerDownRight
  return (
    <details className="ml-4 min-w-0 border-l border-line pl-3 sm:ml-5 [&[open]>summary>.span-chevron]:rotate-180">
      <summary className="flex cursor-pointer list-none items-center gap-2 py-2.5 pr-4 text-[12px] hover:bg-canvas/60 [&::-webkit-details-marker]:hidden">
        <Icon size={14} className={cn('shrink-0 text-muted', span.status === 'error' && 'text-nopagar')} />
        <span className="min-w-0 flex-1 break-words">{humanize(span.step)}</span>
        <span className="flex shrink-0 items-center gap-1 text-[10px] text-muted">
          <Circle size={6} className={cn('fill-current', span.status === 'ok' ? 'text-pagar' : span.status === 'error' ? 'text-nopagar' : 'text-faint')} />
          {span.status}
        </span>
        <span className="shrink-0 font-mono text-[10px] tabular-nums text-faint">{duration}</span>
        <ChevronDown size={12} className="span-chevron shrink-0 text-faint" />
      </summary>
      {span.data && Object.keys(span.data).length ? (
        <div className="pb-3 pr-4">
          <JsonHighlight value={span.data} className="max-h-80 rounded-lg" />
        </div>
      ) : null}
      {span.children.map((child) => (
        <SpanBlock key={child.span_id} span={child} />
      ))}
    </details>
  )
}

function DecisionStat({ label: text, value }: { label: string; value: number }) {
  return (
    <div className="bg-surface px-3 py-2.5">
      <p className="font-mono text-[17px] tracking-[-0.04em] tabular-nums">{value}</p>
      <p className="mt-0.5 text-[10px] text-muted">{text}</p>
    </div>
  )
}

function Block({
  title,
  count,
  icon: Icon,
  meta,
  children,
  openByDefault,
}: {
  title: string
  count?: number
  icon: LucideIcon
  meta?: string
  children: ReactNode
  openByDefault?: boolean
}) {
  const [open, setOpen] = useState(Boolean(openByDefault))
  const id = useId()
  return (
    <section className="min-w-0 border-b border-hairline last:border-b-0">
      <h4>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((value) => !value)}
          className="flex w-full items-center gap-3 px-4 py-4 text-left hover:bg-canvas/60"
        >
          <Icon size={16} strokeWidth={1.6} className="shrink-0 text-muted" />
          <span className="min-w-0 flex-1 text-[13px] font-medium text-ink">{title}</span>
          {count != null ? <span className="min-w-6 rounded-md bg-canvas px-1.5 py-0.5 text-center text-[11px] tabular-nums text-muted">{count}</span> : null}
          {meta ? <span className="text-right text-[10.5px] text-muted">{meta}</span> : null}
          <ChevronDown size={14} strokeWidth={1.5} className={cn('shrink-0 text-muted transition-transform motion-reduce:transition-none', open && 'rotate-180')} />
        </button>
      </h4>
      <div id={id} hidden={!open} className="min-w-0 border-t border-hairline bg-canvas/25">
        {open ? children : null}
      </div>
    </section>
  )
}

/** Structured readings sometimes arrive as a JSON string; preserve their shape for display. */
function structuredValue(value: unknown): object | null {
  if (value !== null && typeof value === 'object') return value
  if (typeof value !== 'string' || !/^\s*[[{]/.test(value)) return null
  try {
    const parsed: unknown = JSON.parse(value)
    return parsed !== null && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

/** Past this, a value is a passage: it goes under its name, two lines until opened. */
const LONG_VALUE = 60

/**
 * What a reviewer needs first: required symbols, then optional ones that were read, then
 * the ones left empty, and last the whole-page transcript. Process order inside each.
 */
function bySignificance(
  symbols: [string, SymbolReading][],
  schema: SymbolIO[],
): [string, SymbolReading][] {
  const meta = new Map(schema.map((symbol, index) => [symbol.name, { symbol, index }]))
  const rank = ([name, reading]: [string, SymbolReading]) => {
    const known = meta.get(name)
    if (known?.symbol.extraction?.source === 'text') return 3
    if (known?.symbol.required) return 0
    return reading.value === null || reading.value === undefined || reading.value === '' ? 2 : 1
  }
  const order = ([name]: [string, SymbolReading]) => meta.get(name)?.index ?? schema.length
  return [...symbols].sort((a, b) => rank(a) - rank(b) || order(a) - order(b))
}

/** The reader marks each page for the models ("[Page 1; reader=native; …]"); a person needs none of it. */
function readable(transcript: string): string {
  return transcript.replace(/\s*\[Page \d+;[^\]]*\]\s*/g, '\n\n').trim()
}
