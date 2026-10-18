import { useEffect, useMemo, useRef, useState, type DragEvent, type RefObject } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { AnimatePresence, motion } from 'motion/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Rocket, Search, Settings2, Upload, X } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { keys } from '../api/queries'
import type { InstanceOut, ProcessDetail, RunSummary, UploadProgress } from '../api/contracts'
import { revokePreview, toPreview, type FilePreview } from '../components/process/FileChip'
import { PublishDraft } from '../components/process/PublishDraft'
import { BatchRunPanel } from '../components/run/BatchRunPanel'
import { CaseDetail } from '../components/inbox/CaseDetail'
import { CaseList } from '../components/inbox/CaseList'
import { MiniCalendar } from '../components/inbox/MiniCalendar'
import { Button, Input, Segmented } from '../components/shell/Controls'
import { EmptyState, ErrorNotice, Notice } from '../components/shell/Notice'
import { Overlay } from '../components/shell/Overlay'
import { StatusBadge } from '../components/shell/StatusBadge'
import { Topbar } from '../components/shell/Topbar'
import { NestedCard } from '../components/shell/Well'
import { cn } from '../lib/cn'
import { formatRunDate, humanize } from '../lib/format'
import { paths } from '../lib/paths'
import { useSession } from '../state/session'
import {
  dayKey,
  formatAmount,
  formatDay,
  parseDay,
  plainReason,
  severity,
  triage,
  type Triage,
} from '../lib/urgency'

type View = 'pendientes' | 'historial'

/** How long a dropped document stays highlighted in the list. */
const FRESH_MS = 9_000

/**
 * The process as its manager sees it: documents that wait for them, most urgent first,
 * a calendar of due dates, and the history of what was decided. Dropping documents on the
 * page reads and decides them; those that need a person land in the list, in their place.
 * Everything else (definition, runs, the review console, settings) is one click away.
 */
export function Inbox() {
  const processId = Number(useParams().processId)
  const [params, setParams] = useSearchParams()
  const view: View = params.get('vista') === 'historial' ? 'historial' : 'pendientes'
  const day = params.get('dia')
  const selectedId = params.get('i') ? Number(params.get('i')) : undefined

  const update = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value == null) merged.delete(key)
      else merged.set(key, value)
    }
    setParams(merged, { replace: true })
  }

  const process = useQuery({
    queryKey: keys.process(processId),
    queryFn: () => api.getProcess(processId),
  })
  const queue = useQuery({
    queryKey: keys.queue(processId, 'all'),
    queryFn: () => api.queue(processId),
  })
  const summary = useQuery({
    queryKey: keys.summary(processId),
    queryFn: () => api.summary(processId),
  })
  const pending = summary.data?.by_status.PENDING ?? 0
  const history = useQuery({
    queryKey: keys.instances(processId),
    queryFn: () => api.listInstances(processId),
    enabled: view === 'historial',
  })
  const today = useReferenceDay(processId)

  const cases = useMemo(() => triage(queue.data ?? [], today.date), [queue.data, today.date])
  const drop = useDrop(processId, today.date)

  const visible = day ? cases.filter((entry) => entry.due && dayKey(entry.due) === day) : cases
  const selected =
    cases.find((entry) => entry.item.id === selectedId) ??
    (history.data ? triage(history.data.filter((item) => item.id === selectedId), today.date)[0] : undefined)

  const [dragging, setDragging] = useState(false)
  const depth = useRef(0)
  const onDrag = {
    onDragEnter: (event: DragEvent) => {
      if (!event.dataTransfer.types.includes('Files')) return
      depth.current += 1
      setDragging(true)
    },
    onDragLeave: () => {
      depth.current = Math.max(0, depth.current - 1)
      if (depth.current === 0) setDragging(false)
    },
    onDragOver: (event: DragEvent) => event.preventDefault(),
    onDrop: (event: DragEvent) => {
      event.preventDefault()
      depth.current = 0
      setDragging(false)
      const files = [...event.dataTransfer.files].filter((file) => /\.(pdf|jpe?g|png|html?)$/i.test(file.name))
      if (files.length) {
        update({ vista: null, dia: null })
        drop.start(files)
      }
    },
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col" {...onDrag}>
      <Topbar
        crumbs={[{ label: 'Procesos', to: paths.processes }, { label: process.data?.name ?? '…' }]}
        actions={
          <>
            <UploadButton disabled={drop.busy} onFiles={drop.start} />
            <Link
              to={paths.panel(processId)}
              title="Panel, definición, ejecuciones, revisión y ajustes"
              className="inline-flex items-center gap-1.5 rounded-full bg-canvas px-3.5 py-1.5 text-[12px] font-medium text-ink ring-1 ring-line hover:bg-well"
            >
              <Settings2 size={12} strokeWidth={1.75} />
              Consola
            </Link>
          </>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-10 pt-4 sm:px-6">
        <header className="mb-5 flex flex-wrap items-center justify-between gap-4">
          <Segmented
            value={view}
            onChange={(next) => update({ vista: next === 'historial' ? next : null, i: null, dia: null })}
            options={[
              { value: 'pendientes', label: 'Te esperan', count: cases.length },
              { value: 'historial', label: 'Historial' },
            ]}
          />
          {view === 'pendientes' && today.cutOff ? (
            <span className="text-[12px] text-muted" title="Fecha de corte del proceso">
              Corte · {formatDay(today.date)}
            </span>
          ) : null}
        </header>


        {process.isError ? <ErrorNotice error={process.error} /> : null}
        {queue.isError ? <ErrorNotice error={queue.error} /> : null}

        {view === 'pendientes' ? (
          <div className="grid max-w-[1120px] gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
            <div className="min-w-0">
              {day ? (
                <p className="mb-2 flex items-center gap-2 text-[12.5px] text-muted">
                  Vencen el {formatDay(parseDay(day))}
                  <button
                    type="button"
                    onClick={() => update({ dia: null })}
                    className="inline-flex items-center gap-1 rounded-full bg-canvas px-2 py-0.5 text-ink ring-1 ring-line"
                  >
                    <X size={11} /> quitar
                  </button>
                </p>
              ) : null}
              {queue.isSuccess && visible.length === 0 ? (
                <section className="rounded-[16px] bg-surface ring-1 ring-line">
                  <EmptyState icon={CheckCircle2} title={day ? 'Nada vence ese día' : pending ? 'Documentos pendientes de evaluar' : 'Sin revisiones pendientes'}>
                    {day
                      ? 'Elige otro día en el calendario o quita el filtro.'
                      : pending
                        ? `${pending} documento${pending === 1 ? '' : 's'} recibido${pending === 1 ? '' : 's'}, pendiente${pending === 1 ? '' : 's'} de evaluación. Aparecerán aquí si necesitan una decisión tuya.`
                        : 'No hay documentos que requieran tu revisión. Puedes consultar los recibidos en Historial.'}
                    {!day && pending > 0 ? (
                      <Link to={paths.panel(processId)} className="mt-3 inline-block font-medium text-ink underline">
                        Ir a la consola para evaluar
                      </Link>
                    ) : null}
                  </EmptyState>
                </section>
              ) : (
                <CaseList
                  cases={visible}
                  decisionTypes={process.data?.decision_types ?? []}
                  selectedId={selectedId}
                  fresh={drop.fresh}
                  onSelect={(id) => update({ i: String(id) })}
                />
              )}
            </div>

            <aside className="space-y-4 lg:sticky lg:top-0 lg:self-start">
              <MiniCalendar
                key={dayKey(today.date)}
                cases={cases}
                today={today.date}
                selected={day}
                onSelect={(next) => update({ dia: next })}
              />
              <Totals cases={cases} />
            </aside>
          </div>
        ) : (
          <History
            items={history.data}
            error={history.error}
            process={process.data}
            today={today.date}
            onSelect={(id) => update({ i: String(id) })}
          />
        )}
      </div>

      <AnimatePresence>
        {dragging ? (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="pointer-events-none absolute inset-3 z-40 grid place-items-center rounded-[24px] border-2 border-dashed border-focus/60 bg-surface/85 backdrop-blur-sm"
          >
            <div className="text-center">
              <Upload size={26} strokeWidth={1.4} className="mx-auto text-focus" />
              <p className="mt-3 text-[18px] font-medium tracking-[-0.03em]">Suelta los documentos</p>
              <p className="mt-1 text-[13px] text-muted">
                Se leen y se deciden ahora. Las que te necesiten entran en tu lista.
              </p>
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>

      {drop.open ? (
        <Overlay onClose={drop.close} size="lg">
          {drop.publishing && drop.draft ? (
            <NestedCard
              label={`Publicar v${drop.nextVersion}`}
              action={
                <Button tone="ghost" onClick={() => drop.setPublishing(false)}>
                  Volver
                </Button>
              }
            >
              <div className="space-y-3 p-4">
                <PublishDraft
                  key={drop.draft.revision}
                  processId={processId}
                  draft={drop.draft}
                  onPublished={drop.published}
                />
              </div>
            </NestedCard>
          ) : (
            <BatchRunPanel
              processId={processId}
              process={process.data}
              runId={undefined}
              queue={drop.files}
              running={drop.busy && drop.progress.length >= drop.files.length * 2}
              uploading={drop.busy && drop.progress.length < drop.files.length * 2}
              finished={drop.batch.isSuccess}
              rulesCount={drop.active}
              startBlocked={drop.startBlocked}
              blockedNotice={
                drop.startBlocked === NO_RULES ? <NoRules processId={processId} drop={drop} /> : undefined
              }
              error={drop.batch.error ? runFailure(drop.batch.error) : undefined}
              progress={drop.progress}
              result={drop.batch.data?.run}
              onFiles={drop.add}
              onRemove={drop.remove}
              onClear={drop.clear}
              onStart={drop.run}
              onClose={drop.close}
            />
          )}
        </Overlay>
      ) : null}

      <AnimatePresence>
        {drop.summary ? <Toast summary={drop.summary} process={process.data} onClose={drop.dismiss} /> : null}
      </AnimatePresence>

      {selected && process.data ? (
        <Overlay onClose={() => update({ i: null })} align="right" size="xl">
          <CaseDetail
            key={selected.item.id}
            process={process.data}
            entry={selected}
            onClose={() => update({ i: null })}
            onResolved={() => update({ i: null })}
          />
        </Overlay>
      ) : null}
    </div>
  )
}


/**
 * The day urgency is measured against. With a cut-off date loaded in the process's
 * parameters, that one, so a demo on past invoices reads the way it did on its day.
 */
function useReferenceDay(processId: number): { date: Date; cutOff: boolean } {
  const sources = useQuery({
    queryKey: keys.sources(processId),
    queryFn: () => api.listSources(processId),
  })
  const loaded = sources.data?.some((source) => source.name === 'parameters') ?? false
  const parameters = useQuery({
    queryKey: keys.source(processId, 'parameters'),
    queryFn: () => api.getSource(processId, 'parameters'),
    enabled: loaded,
    retry: false,
  })
  return useMemo(() => {
    const cutOff = parseDay(parameters.data?.data[0]?.cut_off_date)
    return cutOff ? { date: cutOff, cutOff: true } : { date: new Date(), cutOff: false }
  }, [parameters.data])
}

type QueuedFile = FilePreview & { file: File }
type Summary = { total: number; waiting: number; run: RunSummary }

const NO_RULES = 'Hace falta al menos una regla activa'

/** How long the panel stays on "Lote completado" before the list takes over. */
const CLOSE_AFTER_MS = 1_200
const TOAST_MS = 6_000

/**
 * The console's batch panel, started by a drop or the upload button. When it finishes,
 * it closes on its own: the list shows the arrivals in their place and a toast sums up.
 */
function useDrop(processId: number, today: Date) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [files, setFiles] = useState<QueuedFile[]>([])
  const [progress, setProgress] = useState<UploadProgress[]>([])
  const [fresh, setFresh] = useState<Set<number>>(new Set())
  const [summary, setSummary] = useState<Summary | null>(null)
  const timers = useRef<number[]>([])

  useEffect(() => () => timers.current.forEach(window.clearTimeout), [])
  const later = (fn: () => void, ms: number) => timers.current.push(window.setTimeout(fn, ms))

  const rules = useQuery({
    queryKey: keys.rules(processId),
    queryFn: () => api.listRules(processId),
  })
  const active = rules.data?.filter((rule) => rule.status === 'active').length ?? 0
  const compiling = rules.data?.some((rule) => rule.status === 'compiling')
  // A published process can intentionally rely on its default outcome without rules.
  // A draft alone never enables execution.
  const { isManager } = useSession()
  const [publishing, setPublishing] = useState(false)
  const execution = useQuery({
    queryKey: keys.execution(processId),
    queryFn: () => api.getExecution(processId),
    enabled: isManager && rules.isSuccess && active === 0,
  })
  const startBlocked = compiling
    ? 'El motor no arranca mientras hay reglas compilando'
    : rules.isSuccess && active === 0 && !execution.data?.version_id
      ? NO_RULES
      : undefined
  const draft = useQuery({
    queryKey: keys.draft(processId),
    queryFn: () => api.getDraft(processId),
    enabled: execution.data?.revision != null,
  })
  const versions = useQuery({
    queryKey: keys.versions(processId),
    queryFn: () => api.listVersions(processId),
    enabled: draft.isSuccess,
  })
  const nextVersion = Math.max(0, ...(versions.data ?? []).map((version) => version.number)) + 1

  const batch = useMutation({
    mutationFn: async (queued: QueuedFile[]) => {
      setProgress([])
      const uploads = await api.uploadFiles(
        processId,
        queued.map((item) => item.file),
        (event) => setProgress((current) => [...current, event]),
      )
      const run = await api.run(processId, uploads.map((upload) => upload.instance_id))
      await queryClient.invalidateQueries()
      const queue = await queryClient.fetchQuery({
        queryKey: keys.queue(processId, 'all'),
        queryFn: () => api.queue(processId),
      })
      return { uploads, run, queue }
    },
    onSuccess: ({ uploads, run, queue }) => {
      const ids = new Set(uploads.map((upload) => upload.instance_id))
      const arrived = triage(queue, today).filter((entry) => ids.has(entry.item.id))
      later(() => {
        clear()
        setOpen(false)
        setFresh(new Set(arrived.map((entry) => entry.item.id)))
        setSummary({ total: uploads.length, waiting: arrived.length, run })
        later(() => setFresh(new Set()), FRESH_MS)
        later(() => setSummary(null), TOAST_MS)
      }, CLOSE_AFTER_MS)
    },
  })

  const clear = () => {
    setFiles((current) => {
      current.forEach(revokePreview)
      return []
    })
  }
  const add = (incoming: File[]) =>
    setFiles((current) => [...current, ...incoming.map((file) => ({ ...toPreview(file), file }))])

  return {
    open,
    files,
    progress,
    fresh,
    summary,
    startBlocked,
    active,
    batch,
    busy: batch.isPending,
    isManager,
    draft: execution.data?.revision != null ? draft.data : undefined,
    nextVersion,
    publishing,
    setPublishing,
    /** The draft is live: the dropped files go through with it at once. */
    published: async () => {
      setPublishing(false)
      await queryClient.invalidateQueries()
      if (files.length) batch.mutate(files)
    },
    /** A drop queues the files and starts at once: dropping them is the intent. */
    start: (incoming: File[]) => {
      if (batch.isPending) return
      batch.reset()
      const queued = incoming.map((file) => ({ ...toPreview(file), file }))
      setFiles(queued)
      setOpen(true)
      if (!startBlocked) batch.mutate(queued)
    },
    add,
    remove: (id: string) =>
      setFiles((current) => {
        const gone = current.find((item) => item.id === id)
        if (gone) revokePreview(gone)
        return current.filter((item) => item.id !== id)
      }),
    clear,
    run: () => {
      if (files.length && !startBlocked) batch.mutate(files)
    },
    close: () => {
      if (batch.isPending) return
      clear()
      setPublishing(false)
      setOpen(false)
    },
    dismiss: () => setSummary(null),
  }
}

/** No active rules: publish the waiting draft from here, or go write some. */
function NoRules({ processId, drop }: { processId: number; drop: ReturnType<typeof useDrop> }) {
  const see = (label: string) => (
    <Link
      to={paths.processChat(processId)}
      className="inline-flex items-center rounded-full bg-canvas px-3 py-1.5 text-[12px] font-medium text-ink ring-1 ring-line hover:bg-well"
    >
      {label}
    </Link>
  )

  if (!drop.draft) {
    return (
      <Notice tone="warning" title="No hay reglas activas" action={see('Ir a Definición')}>
        Escribe las reglas del proceso y publícalas para decidir estos documentos.
      </Notice>
    )
  }
  return (
    <Notice
      tone="warning"
      title="No hay reglas activas"
      action={
        <div className="flex items-center gap-1.5">
          {see('Ver')}
          {drop.isManager ? (
            <Button tone="primary" onClick={() => drop.setPublishing(true)} className="group">
              <Rocket
                size={12}
                strokeWidth={2}
                className="transition-transform duration-200 group-hover:-translate-y-px group-hover:translate-x-px motion-reduce:transition-none"
              />
              Publicar v{drop.nextVersion}
            </Button>
          ) : null}
        </div>
      }
    >
      {drop.isManager
        ? 'Hay un borrador listo. Publícalo y estos documentos se procesan a continuación.'
        : 'Hay un borrador listo. Pide a un responsable que lo publique.'}
    </Notice>
  )
}

/** The backend refuses a run with nothing published; say what to do about it. */
function runFailure(error: unknown): unknown {
  if (error instanceof ApiError && error.status === 409 && /publish an approved/i.test(error.message)) {
    return new ApiError(error.status, error.code, 'Publica una versión desde la Consola antes de procesar documentos')
  }
  return error
}

/** One line after a batch: how many went through and how many wait on the manager. */
function Toast({
  summary,
  process,
  onClose,
}: {
  summary: Summary
  process: ProcessDetail | undefined
  onClose: () => void
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 8 }}
      transition={{ duration: 0.25, ease: [0.23, 1, 0.32, 1] }}
      className="fixed bottom-6 right-6 z-40 flex max-w-sm items-center gap-3 rounded-[14px] bg-surface py-3 pl-4 pr-2 shadow-pop ring-1 ring-line"
    >
      <CheckCircle2 size={16} strokeWidth={1.6} className="shrink-0 text-pagar" />
      <div className="min-w-0 text-[13px]">
        <p className="text-ink">
          {summary.total} procesada{summary.total === 1 ? '' : 's'} ·{' '}
          {summary.waiting
            ? `${summary.waiting} te necesita${summary.waiting === 1 ? '' : 'n'}`
            : 'ninguna te necesita'}
        </p>
        <p className="mt-1 flex flex-wrap gap-1">
          {Object.entries(summary.run.by_decision).map(([name, count]) => (
            <StatusBadge key={name} value={name} decisionTypes={process?.decision_types}>
              {`${name.replaceAll('_', ' ')} ${count}`}
            </StatusBadge>
          ))}
        </p>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="Cerrar"
        className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-muted hover:bg-canvas hover:text-ink"
      >
        <X size={13} strokeWidth={1.75} />
      </button>
    </motion.div>
  )
}

function Totals({ cases }: { cases: Triage[] }) {
  const overdue = cases.filter((entry) => entry.flags.includes('overdue'))
  const urgent = cases.filter((entry) => severity(entry.flags) === 'medium')
  const amounts = cases.flatMap((entry) => entry.amount == null ? [] : [entry.amount])
  const amount = amounts.length ? amounts.reduce((sum, value) => sum + value, 0) : cases.length ? null : 0
  const cells = [
    { label: 'Vencidas', value: String(overdue.length), tone: overdue.length ? 'text-nopagar' : 'text-ink' },
    { label: 'Urgentes', value: String(urgent.length), tone: urgent.length ? 'text-urgent' : 'text-ink' },
    { label: amounts.length < cases.length ? 'Importe conocido' : 'Importe en espera', value: formatAmount(amount), tone: 'text-ink' },
  ]
  return (
    <section className="divide-y divide-hairline rounded-[16px] bg-surface ring-1 ring-line">
      {cells.map((cell) => (
        <div key={cell.label} className="flex items-baseline justify-between px-4 py-2.5">
          <span className="text-[12.5px] text-muted">{cell.label}</span>
          <span className={cn('font-mono text-[14px] tabular-nums', cell.tone)}>{cell.value}</span>
        </div>
      ))}
    </section>
  )
}

function UploadButton({ disabled, onFiles }: { disabled: boolean; onFiles: (files: File[]) => void }) {
  const input = useRef<HTMLInputElement>(null)
  return (
    <>
      <Button tone="soft" disabled={disabled} onClick={() => input.current?.click()}>
        <Upload size={12} strokeWidth={1.75} />
        {disabled ? 'Procesando…' : 'Subir documentos'}
      </Button>
      <FileInput ref={input} onFiles={onFiles} />
    </>
  )
}

function FileInput({
  ref,
  onFiles,
}: {
  ref: RefObject<HTMLInputElement | null>
  onFiles: (files: File[]) => void
}) {
  return (
    <input
      ref={ref}
      type="file"
      accept=".pdf,.jpg,.jpeg,.png,.html,.htm,application/pdf,image/jpeg,image/png,text/html"
      multiple
      hidden
      onChange={(event) => {
        const files = [...(event.target.files ?? [])]
        event.target.value = ''
        if (files.length) onFiles(files)
      }}
    />
  )
}

/** Every decided document, newest decision first, searchable and filtered by outcome. */
function History({
  items,
  error,
  process,
  today,
  onSelect,
}: {
  items: InstanceOut[] | undefined
  error: unknown
  process: ProcessDetail | undefined
  today: Date
  onSelect: (id: number) => void
}) {
  const [query, setQuery] = useState('')
  const [outcome, setOutcome] = useState('all')

  const rows = useMemo(() => {
    const decided = (items ?? []).filter((item) => item.decision && item.decided_at)
    return triage(decided, today)
      .filter((entry) => outcome === 'all' || entry.item.decision === outcome)
      .filter((entry) => {
        const q = query.trim().toLowerCase()
        if (!q) return true
        return [entry.item.name, entry.party, entry.number].some((value) => value?.toLowerCase().includes(q))
      })
      .sort((a, b) => (b.item.decided_at ?? '').localeCompare(a.item.decided_at ?? ''))
  }, [items, today, outcome, query])

  if (error) return <ErrorNotice error={error} />

  return (
    <section>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label className="relative min-w-[220px] flex-1">
          <Search size={13} strokeWidth={1.75} className="absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Nombre o archivo"
            className="pl-8"
          />
        </label>
        <Segmented
          value={outcome}
          onChange={setOutcome}
          options={[
            { value: 'all', label: 'Todas' },
            ...(process?.decision_types ?? []).map((type) => ({ value: type.name, label: humanize(type.name) })),
          ]}
        />
      </div>

      <div className="overflow-x-auto rounded-[16px] bg-surface ring-1 ring-line">
        {!items ? (
          <p className="px-4 py-5 text-[13px] text-muted">Cargando…</p>
        ) : rows.length === 0 ? (
          <p className="px-4 py-5 text-[13px] text-muted">Nada con esos filtros.</p>
        ) : (
          <table className="w-full min-w-[560px] text-left text-[13px]">
            <thead className="border-b border-hairline text-[11px] text-faint">
              <tr>
                <th className="px-4 py-2 font-normal">Documento</th>
                <th className="px-4 py-2 text-right font-normal">Importe</th>
                <th className="px-4 py-2 font-normal">Decisión</th>
                <th className="hidden px-4 py-2 font-normal md:table-cell">Motivo</th>
                <th className="px-4 py-2 font-normal">Quién · cuándo</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {rows.map((entry) => (
                <tr
                  key={entry.item.id}
                  onClick={() => onSelect(entry.item.id)}
                  className="cursor-pointer hover:bg-canvas"
                >
                  <td className="max-w-[220px] px-4 py-2.5">
                    <p className="truncate text-ink">{entry.party ?? entry.item.name}</p>
                    <p className="truncate font-mono text-[11px] text-faint">{entry.number ?? entry.item.name}</p>
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums">{formatAmount(entry.amount)}</td>
                  <td className="px-4 py-2.5">
                    <StatusBadge value={entry.item.decision!} decisionTypes={process?.decision_types} />
                  </td>
                  <td className="hidden max-w-[320px] truncate px-4 py-2.5 text-muted md:table-cell">
                    {plainReason(entry.item, process?.decision_types ?? []).title}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2.5 text-[12px] text-muted">
                    {entry.item.author === 'engine' ? 'Proceso' : entry.item.author}
                    <span className="text-faint"> · {formatRunDate(entry.item.decided_at!)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}
