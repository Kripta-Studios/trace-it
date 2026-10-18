import { RunCost } from '../components/run/RunCost'
import { useMemo, useState, type ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSession } from '../state/session'
import {
  AlertTriangle,
  BookOpenText,
  ChevronDown,
  ChevronRight,
  Cpu,
  Download,
  FileText,
  PencilLine,
  Play,
  Rocket,
} from 'lucide-react'
import { api, ApiError } from '../api/client'
import { keys } from '../api/queries'
import { MetricCells, PlaneDashboards } from '../components/process/PlaneDashboards'
import { PublishDraft } from '../components/process/PublishDraft'
import { ReprocessAfterPublish } from '../components/process/ReprocessAfterPublish'
import { ExportButton } from '../components/process/ExportButton'
import { ProcessAbout } from '../components/process/ProcessAbout'
import {
  revokePreview,
  toPreview,
  type FilePreview,
} from '../components/process/FileChip'
import { ProcessScreen } from '../components/process/ProcessScreen'
import { BatchRunPanel } from '../components/run/BatchRunPanel'
import { Button } from '../components/shell/Controls'
import { Overlay } from '../components/shell/Overlay'
import { ErrorNotice } from '../components/shell/Notice'
import { NestedCard } from '../components/shell/Well'
import { cn } from '../lib/cn'
import { ALERTS_TAB, paths } from '../lib/paths'
import type { VersionOut, RunOut, UploadProgress, ExecutionMetrics, NormRule, ProcessDetail, ProcessMetrics, ProcessSummary, Proposal, Rule } from '../api/contracts'
import { t } from '../i18n'
import { formatEuro, formatMs, formatRunDate } from '../lib/format'
import { decisionTone, type DecisionTone } from '../lib/process'

type QueuedFile = FilePreview & { file: File }

export function Process() {
  const processId = Number(useParams().processId)
  const queryClient = useQueryClient()
  const { isManager } = useSession()
  const [runPanelOpen, setRunPanelOpen] = useState(false)
  const [queue, setQueue] = useState<QueuedFile[]>([])
  // Every upload event of the batch, in order: the panel's live output.
  const [progress, setProgress] = useState<UploadProgress[]>([])
  const [published, setPublished] = useState<number | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const publishOpen = searchParams.get('publicar') === '1'

  const process = useQuery({
    queryKey: keys.process(processId),
    queryFn: () => api.getProcess(processId),
  })
  const summary = useQuery({
    queryKey: keys.summary(processId),
    queryFn: () => api.summary(processId),
  })
  const plane = useQuery({
    queryKey: keys.planeMetrics(processId, 'execution'),
    queryFn: () => api.planeMetrics(processId, 'execution'),
  })
  const providers = useQuery({
    queryKey: keys.processMetrics(processId),
    queryFn: () => api.processMetrics(processId),
  })
  // `revision` is the draft's, or null when there is none; only then is there a draft to read.
  const execution = useQuery({
    queryKey: keys.execution(processId),
    queryFn: () => api.getExecution(processId),
    enabled: isManager,
  })
  const hasDraft = execution.data?.revision != null
  const draft = useQuery({
    queryKey: keys.draft(processId),
    queryFn: () => api.getDraft(processId),
    enabled: hasDraft,
  })
  const versions = useQuery({
    queryKey: keys.versions(processId),
    queryFn: () => api.listVersions(processId),
  })
  const rules = useQuery({
    queryKey: keys.rules(processId),
    queryFn: () => api.listRules(processId),
    refetchInterval: (query) =>
      query.state.data?.some((rule) => rule.status === 'compiling') ? 2_000 : false,
  })
  const norm = useQuery({
    queryKey: keys.norm(processId),
    queryFn: () => api.listNormRules(processId),
  })
  const runs = useQuery({
    queryKey: keys.runs(processId),
    queryFn: () => api.listRuns(processId),
  })
  // A sync or a publication opens alerts in the background, so look again every 10 s.
  const alerts = useQuery({
    queryKey: keys.alerts(processId, 'open'),
    queryFn: () => api.listAlerts(processId, 'open'),
    refetchInterval: 10_000,
  })
  // Escalation proposals live on their case and count as documents waiting for a person.
  const proposals = useQuery({
    queryKey: keys.proposals(processId, 'open'),
    queryFn: () => api.listProposals(processId, 'open'),
    enabled: isManager,
    select: (items) => items.filter((item) => item.channel !== 'escalation'),
  })
  // reviewer-agent FE-5 (docs/reviewer-agent.md): open rule suggestions live on their case, so
  // the Panel counts them and links to that case. If merging a newer version from Carlos, keep his
  // UI and preserve: escalation proposals link to review?i=<instance_id>; the count is
  // kind=rule, channel=escalation, status=open. Same cache as `proposals`, another view of it.
  const ruleSuggestions = useQuery({
    queryKey: keys.proposals(processId, 'open'),
    queryFn: () => api.listProposals(processId, 'open'),
    enabled: isManager,
    select: (items) => items.filter((item) => item.channel === 'escalation' && item.kind === 'rule'),
  })
  const findings = useQuery({
    queryKey: keys.findings(processId),
    queryFn: () => api.listFindings(processId),
  })

  const run = useMutation({
    mutationFn: (instanceIds: number[]) => api.run(processId, instanceIds),
    // Everything counts instances, and the new run joins the history.
    onSuccess: () => void queryClient.invalidateQueries(),
  })

  const upload = useMutation({
    mutationFn: (incoming: File[]) => {
      setProgress([])
      return api.uploadFiles(processId, incoming, (event) =>
        setProgress((current) => [...current, event]),
      )
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.instances(processId) })
      void queryClient.invalidateQueries({ queryKey: keys.summary(processId) })
    },
  })

  const active = rules.data?.filter((rule) => rule.status === 'active').length ?? 0
  const compiling = rules.data?.filter((rule) => rule.status === 'compiling').length ?? 0
  const waiting = summary.data?.queue ?? 0
  const currentVersion = Math.max(0, ...(versions.data ?? []).map((version) => version.number))
  const latestPublished = versions.data?.find((version) => version.number === currentVersion)
  const nextVersion = currentVersion + 1
  const busy = run.isPending || upload.isPending
  const startBlocked =
    compiling > 0
      ? 'El motor no arranca mientras hay reglas compilando'
      : active === 0 && !latestPublished
        ? 'Hace falta al menos una regla activa'
        : undefined

  const addToQueue = (incoming: File[]) => {
    setQueue((current) => [...current, ...incoming.map(toPreview)])
  }

  const removeFromQueue = (id: string) => {
    setQueue((current) => {
      const gone = current.find((item) => item.id === id)
      if (gone) revokePreview(gone)
      return current.filter((item) => item.id !== id)
    })
  }

  const clearQueue = () => {
    queue.forEach(revokePreview)
    setQueue([])
  }

  const openRun = () => {
    if (busy) {
      setRunPanelOpen(true)
      return
    }
    if (run.isSuccess || !runPanelOpen) {
      run.reset()
      upload.reset()
      clearQueue()
    }
    setRunPanelOpen(true)
  }

  const closeRun = () => {
    if (busy) return
    if (!run.isSuccess) clearQueue()
    else {
      queue.forEach(revokePreview)
      setQueue([])
    }
    setRunPanelOpen(false)
  }

  const startRun = async () => {
    if (!queue.length || startBlocked) return
    try {
      const uploaded = await upload.mutateAsync(queue.map((item) => item.file))
      await run.mutateAsync(uploaded.map((item) => item.instance_id))
    } catch {
      // el panel muestra el error
    }
  }

  if (process.isError) {
    return (
      <ProcessScreen
        processId={processId}
        crumbs={[{ label: 'Procesos', to: paths.processes }, { label: String(processId) }]}
      >
        <div className="px-4 py-6 sm:px-6">
          <ErrorNotice error={process.error} />
        </div>
      </ProcessScreen>
    )
  }

  return (
    <ProcessScreen
      processId={processId}
      crumbs={[{ label: 'Procesos', to: paths.processes }, { label: process.data?.name ?? '…' }]}
      actions={
        <>
          <Button
            tone="soft"
            onClick={openRun}
            disabled={busy}
            title="Elige el lote y lo encolas. El motor arranca después."
          >
            <Play size={12} strokeWidth={2} />
            {busy ? 'Ejecutando…' : 'Ejecutar'}
          </Button>
          <ExportButton processId={processId} />
        </>
      }
    >
      {runPanelOpen ? (
        <Overlay onClose={closeRun} size="lg">
          <BatchRunPanel
            processId={processId}
            process={process.data}
            runId={run.isSuccess && !runs.isFetching ? runs.data?.[0]?.id : undefined}
            queue={queue}
            running={run.isPending}
            uploading={upload.isPending}
            finished={run.isSuccess}
            rulesCount={active}
            startBlocked={startBlocked}
            error={runFailure(upload.error ?? run.error)}
            progress={progress}
            result={run.data}
            onFiles={addToQueue}
            onRemove={removeFromQueue}
            onClear={clearQueue}
            onStart={() => void startRun()}
            onClose={closeRun}
          />
        </Overlay>
      ) : null}

      {publishOpen && hasDraft && draft.data ? (
        <Overlay onClose={() => setSearchParams({}, { replace: true })} size="lg">
          <NestedCard
            label={`Publicar v${nextVersion}`}
            action={
              <Button tone="ghost" onClick={() => setSearchParams({}, { replace: true })}>
                Cerrar
              </Button>
            }
          >
            <div className="space-y-3 p-4">
              <PublishDraft
                key={draft.data.revision}
                processId={processId}
                draft={draft.data}
                onPublished={(version) => {
                  setPublished(version.number)
                  setSearchParams({}, { replace: true })
                }}
              />
            </div>
          </NestedCard>
        </Overlay>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-10 pt-4 sm:px-6">
        {published != null ? (
          <div className="mb-6">
            <ReprocessAfterPublish
              processId={processId}
              version={published}
              onClose={() => setPublished(null)}
            />
          </div>
        ) : null}
        <header className="mb-6">
          <VersionChips
            processId={processId}
            published={latestPublished}
            draft={hasDraft && draft.data ? nextVersion : undefined}
          />
          <h1 className="mt-2 text-[32px] font-medium leading-[1.1] tracking-[-0.045em]">
            {process.data?.name ?? '…'}
          </h1>
          <ProcessAbout
            key={processId}
            name={process.data?.name ?? ''}
            description={process.data?.description ?? ''}
          />
        </header>

        <Alerts
          processId={processId}
          waiting={waiting}
          compiling={compiling}
          findings={findings.data?.length ?? 0}
          draftVersion={hasDraft && draft.data ? nextVersion : undefined}
          alerts={alerts.data?.length ?? 0}
          proposals={proposals.data?.length ?? 0}
          ruleSuggestions={ruleSuggestions.data ?? []}
        />

        {summary.data?.instances === 0 ? (
          <EmptyPanel processId={processId} published={currentVersion > 0} onRun={openRun} />
        ) : (
          <>
            <Metrics summary={summary.data} />

            <Split summary={summary.data} process={process.data} />

            <Runs
              processId={processId}
              runs={runs.data ?? []}
              error={runs.error}
              documentCount={summary.data?.instances ?? 0}
            />
          </>
        )}

        <TechnicalDetails>
          <Pipeline
            total={summary.data?.instances ?? 0}
            norm={norm.data ?? []}
            rules={rules.data ?? []}
            decided={summary.data?.by_status.DECIDED ?? 0}
            pending={summary.data?.by_status.PENDING ?? 0}
          />
          <Performance plane={plane.data} providers={providers.data} />
          <PlaneDashboards processId={processId} decisionTypes={process.data?.decision_types} />
        </TechnicalDetails>
      </div>
    </ProcessScreen>
  )
}

/** The backend refuses a run with nothing published; say what to do about it. */
function runFailure(error: unknown): unknown {
  if (error instanceof ApiError && error.status === 409 && /publish an approved/i.test(error.message)) {
    return new ApiError(error.status, error.code, 'Publica una versión antes de ejecutar')
  }
  return error
}

function Alerts({
  processId,
  waiting,
  compiling,
  findings,
  draftVersion,
  alerts,
  proposals,
  ruleSuggestions,
}: {
  processId: number
  waiting: number
  compiling: number
  findings: number
  draftVersion: number | undefined
  alerts: number
  proposals: number
  ruleSuggestions: Proposal[]
}) {
  const items = [
    waiting > 0
      ? {
          to: paths.review(processId),
          text: `${waiting} documento${waiting === 1 ? '' : 's'} esperan a una persona`,
        }
      : null,
    compiling > 0
      ? {
          to: paths.definitionManual(processId),
          text: `${compiling} regla${compiling === 1 ? '' : 's'} compilando. El motor no arranca hasta que terminen.`,
        }
      : null,
    proposals > 0
      ? {
          to: paths.processChat(processId),
          text: `${proposals} propuesta${proposals === 1 ? ' espera' : 's esperan'} tu decisión`,
        }
      : null,
    ruleSuggestions[0]?.instance_id != null
      ? {
          to: paths.reviewCase(processId, ruleSuggestions[0].instance_id),
          text: `${ruleSuggestions.length} ${t(ruleSuggestions.length === 1 ? 'reviewerAgent.ruleSuggestion' : 'reviewerAgent.ruleSuggestions')}`,
        }
      : null,
    alerts > 0
      ? {
          to: `${paths.review(processId)}?tipo=${ALERTS_TAB}`,
          text: `${alerts} decisi${alerts === 1 ? 'ón podría' : 'ones podrían'} cambiar`,
        }
      : null,
    draftVersion != null
      ? {
          to: `${paths.panel(processId)}?publicar=1`,
          text: `Borrador sin publicar · Publicar v${draftVersion}`,
        }
      : null,
    findings > 0
      ? {
          to: paths.versions(processId),
          text: `${findings} decisión${findings === 1 ? '' : 'es'} del pasado contradicen la norma de hoy`,
        }
      : null,
  ].filter((item): item is { to: string; text: string } => item != null)

  if (items.length === 0) return null

  return (
    <section className="mb-6 overflow-hidden rounded-[16px] bg-surface ring-1 ring-line">
      <div className="flex items-center gap-2 px-4 py-2.5">
        <AlertTriangle size={14} strokeWidth={1.6} className="text-escalar" />
        <p className="text-[13px] font-medium text-ink">Necesita tu atención</p>
      </div>
      <ul className="divide-y divide-hairline border-t border-hairline">
        {items.map((item) => (
          <li key={item.to}>
            <Link
              to={item.to}
              className="flex items-center justify-between gap-4 px-4 py-2.5 text-[13px] text-ink hover:bg-canvas"
            >
              {item.text}
              <ChevronRight size={13} strokeWidth={1.6} className="shrink-0 text-faint" />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}

function Metrics({ summary }: { summary: ProcessSummary | undefined }) {
  const cells = [
    { label: 'Documentos', value: String(summary?.instances ?? '—'), note: 'recibidos' },
    { label: 'Decididos', value: String(summary?.by_status.DECIDED ?? '—'), note: 'con decisión' },
    { label: 'Esperan revisión', value: String(summary?.queue ?? '—'), note: 'necesitan a una persona' },
  ]

  return <MetricCells cells={cells} />
}

/** How long a run takes and what the providers cost: for whoever runs the platform. */
function Performance({
  plane,
  providers,
}: {
  plane: ExecutionMetrics | undefined
  providers: ProcessMetrics | undefined
}) {
  const latency = plane?.steps.find((step) => step.step === 'run_process')?.p50_ms
  const cost = providers?.providers.reduce((sum, item) => sum + item.known_cost_usd, 0)
  const cells = [
    {
      label: 'Latencia',
      value: latency != null ? formatMs(latency) : '—',
      note: latency != null ? 'mediana por ejecución' : 'aún no hay ejecuciones',
    },
    {
      label: 'Coste',
      value: cost != null ? formatEuro(cost) : '—',
      note: 'coste conocido de proveedores (USD)',
    },
  ]

  return <MetricCells cells={cells} />
}

/** Engine stages, latency, cost and the per-plane dashboards: closed until asked for. */
function TechnicalDetails({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)

  return (
    <section className="mt-10 border-t border-hairline pt-5">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((next) => !next)}
        className="group flex w-full items-center justify-between gap-4 text-left"
      >
        <div>
          <h2 className="text-[15px] font-medium tracking-[-0.02em]">Detalles técnicos</h2>
          <p className="mt-0.5 text-[12.5px] text-muted">
            Etapas del motor, latencia, coste y trazabilidad.
          </p>
        </div>
        <ChevronDown
          size={15}
          strokeWidth={1.6}
          className={cn('shrink-0 text-faint transition-transform group-hover:text-ink', open && 'rotate-180')}
        />
      </button>
      {open ? <div className="mt-5">{children}</div> : null}
    </section>
  )
}

/** ESCALAR first: it is what a rerun after learning should shrink. */
function runSplit(run: RunOut): string {
  return Object.entries(run.by_decision)
    .sort(([a], [b]) => (a === 'ESCALAR' ? -1 : b === 'ESCALAR' ? 1 : a.localeCompare(b)))
    .map(([name, value]) => `${value} ${name.replaceAll('_', ' ')}`)
    .join(', ')
}

const RECENT_RUNS = 5

function Runs({
  processId,
  runs,
  error,
  documentCount,
}: {
  processId: number
  runs: RunOut[]
  error: unknown
  documentCount: number
}) {
  return (
    <section className="mt-8">
      <div className="mb-3 flex items-end justify-between gap-4">
        <div>
          <h2 className="text-[18px] font-medium tracking-[-0.03em]">Ejecuciones</h2>
          <p className="mt-1 text-[12.5px] text-muted">
            Los últimos lotes procesados. Abre uno para ver sus documentos.
          </p>
        </div>
        <Link to={paths.instances(processId)} className="text-[12px] text-muted hover:text-ink">
          {runs.length > RECENT_RUNS ? `Ver las ${runs.length}` : 'Historial'}
        </Link>
      </div>
      <div className="overflow-hidden rounded-[16px] bg-surface ring-1 ring-line">
        {error ? (
          <ErrorNotice error={error} />
        ) : runs.length === 0 ? (
          <p className="px-4 py-5 text-[13px] text-muted">
            {documentCount
              ? `${documentCount} documentos en el proceso. Ejecutar abre la cola del lote.`
              : 'Aún no se ha ejecutado este proceso.'}
          </p>
        ) : (
          <ul className="divide-y divide-hairline">
            {runs.slice(0, RECENT_RUNS).map((item) => (
              <li key={item.id}>
                <Link
                  to={paths.instances(processId, item.id)}
                  className="flex items-center justify-between gap-4 px-4 py-3 hover:bg-canvas"
                >
                  <div className="min-w-0">
                    <p className="text-[13px] text-ink">
                      {item.decided} decisiones · v{item.version_number}
                    </p>
                    <RunCost cost={item.cost} />
                    <p className="truncate text-[12px] text-muted">
                      {runSplit(item) || 'Sin salidas'}
                      {item.author ? ` · ${item.author}` : ''}
                    </p>
                  </div>
                  <span className="shrink-0 font-mono text-[11px] text-faint">
                    {formatRunDate(item.started_at)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}

function Pipeline({
  total,
  norm,
  rules,
  decided,
  pending,
}: {
  total: number
  norm: NormRule[]
  rules: Rule[]
  decided: number
  pending: number
}) {
  const checks = norm.reduce((sum, item) => sum + item.rules.length, 0)
  const compiling = rules.filter((rule) => rule.status === 'compiling').length
  const active = rules.filter((rule) => rule.status === 'active').length

  const stages = [
    { icon: FileText, label: 'Documentos', value: total, note: 'instancias' },
    { icon: BookOpenText, label: 'Norma', value: norm.length, note: `${checks} comprobaciones` },
    {
      icon: Cpu,
      label: 'Compilador',
      value: active,
      note: compiling ? `${compiling} compilando` : 'activas',
    },
    { icon: Play, label: 'Motor', value: decided, note: pending ? `${pending} pendientes` : 'cerrado' },
    { icon: Download, label: 'Exportación', value: decided, note: 'líneas listas' },
  ]

  return (
    <section className="mb-6">
      <p className="mb-3 font-mono text-[11px] tracking-[0.12em] text-faint">
        ESTADO DEL PROCESO
      </p>
      <div className="grid overflow-hidden rounded-[16px] bg-surface ring-1 ring-line sm:grid-cols-5">
        {stages.map((stage, index) => (
          <div
            key={stage.label}
            className="relative border-b border-hairline px-3.5 py-3 last:border-0 sm:border-b-0 sm:border-r sm:last:border-r-0"
          >
            <div className="flex items-center gap-1.5 text-[11px] text-muted">
              <stage.icon size={12} strokeWidth={1.6} />
              <span>
                {index + 1} · {stage.label}
              </span>
            </div>
            <p className="mt-2 font-mono text-[22px] tracking-[-0.04em] tabular-nums">
              {stage.value}
            </p>
            <p className="mt-0.5 font-mono text-[10px] text-faint">{stage.note}</p>
          </div>
        ))}
      </div>
    </section>
  )
}

const TONE_DOT: Record<DecisionTone | 'pending', string> = {
  positive: 'bg-pagar',
  negative: 'bg-nopagar',
  attention: 'bg-escalar',
  pending: 'bg-faint/50',
}

function Split({
  summary,
  process,
}: {
  summary: ProcessSummary | undefined
  process: ProcessDetail | undefined
}) {
  const cells = useMemo(() => {
    if (!summary) return []
    const outcomes = Object.entries(summary.by_decision)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([label, value]) => ({ label, value, tone: decisionTone(process, label) }))
    const pending = summary.by_status.PENDING ?? 0
    return pending > 0
      ? [...outcomes, { label: t('instanceStatus.PENDING'), value: pending, tone: 'pending' as const }]
      : outcomes
  }, [summary, process])

  if (cells.length === 0) return null

  return (
    <section className="rounded-[16px] bg-surface px-5 py-4 ring-1 ring-line">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <h2 className="text-[15px] font-medium tracking-[-0.02em]">Decisiones</h2>
        <ul className="flex flex-wrap items-center gap-x-4 gap-y-1">
          {cells.map((cell) => (
            <li key={cell.label} className="flex items-center gap-1.5 text-[12.5px]">
              <span className={cn('h-2.5 w-2.5 rounded-[3px]', TONE_DOT[cell.tone])} />
              <span className="text-muted">{humanize(cell.label)}</span>
              <span className="font-mono tabular-nums text-ink">{cell.value}</span>
            </li>
          ))}
        </ul>
      </div>
      <div className="mt-4 flex flex-wrap gap-1">
        {cells.flatMap((cell) =>
          Array.from({ length: cell.value }, (_, index) => (
            <span
              key={`${cell.label}-${index}`}
              title={humanize(cell.label)}
              className={cn(
                'h-3 w-3 rounded-[3px] transition-transform hover:scale-125',
                TONE_DOT[cell.tone],
              )}
            />
          )),
        )}
      </div>
    </section>
  )
}

/** NO_PAGAR reads as "No pagar". */
function humanize(label: string): string {
  const words = label.replaceAll('_', ' ').toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** A process with no documents yet: one next step, nothing to read around it. */
function EmptyPanel({
  processId,
  published,
  onRun,
}: {
  processId: number
  published: boolean
  onRun: () => void
}) {
  return (
    <section className="flex flex-col items-center rounded-[16px] bg-surface px-6 py-12 text-center ring-1 ring-line">
      <FileText size={20} strokeWidth={1.4} className="text-faint" />
      <h2 className="mt-3 text-[16px] font-medium tracking-[-0.02em]">Aún no hay documentos</h2>
      <p className="mt-1 max-w-sm text-[13px] leading-5 text-muted">
        {published
          ? 'Sube un lote y el proceso decide cada documento con la versión publicada.'
          : 'Define las reglas y publica una versión. Después, sube un lote.'}
      </p>
      <div className="mt-5">
        {published ? (
          <Button tone="primary" onClick={onRun}>
            <Play size={12} strokeWidth={2} />
            Ejecutar un lote
          </Button>
        ) : (
          <Link
            to={paths.processChat(processId)}
            className="inline-flex items-center gap-1.5 rounded-full bg-ink px-3.5 py-1.5 text-[12px] font-medium text-on-ink hover:bg-ink/90"
          >
            Ir a Definición
          </Link>
        )}
      </div>
    </section>
  )
}

/** Which version decides today, and whether a draft waits to be published. */
function VersionChips({
  processId,
  published,
  draft,
}: {
  processId: number
  published: VersionOut | undefined
  draft: number | undefined
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {published ? (
        <span
          title={published.reason || undefined}
          className="inline-flex items-center gap-1.5 rounded-full bg-pagar-soft py-1 pl-2 pr-2.5 text-[11.5px] text-pagar"
        >
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-pagar opacity-40 motion-reduce:animate-none" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-pagar" />
          </span>
          <span className="font-mono font-medium">v{published.number}</span>
          <span>en vigor</span>
          <span className="text-pagar/60">· {formatRunDate(published.created_at)}</span>
        </span>
      ) : draft == null ? (
        <span className="inline-flex items-center gap-1.5 rounded-full bg-canvas px-2.5 py-1 text-[11.5px] text-muted ring-1 ring-line">
          <span className="h-1.5 w-1.5 rounded-full bg-faint/60" />
          Sin versión publicada
        </span>
      ) : null}
      {draft != null ? (
        <>
          <span className="inline-flex items-center gap-1.5 rounded-full bg-escalar-soft py-1 pl-2 pr-2.5 text-[11.5px] text-escalar">
            <PencilLine size={11} strokeWidth={2} />
            <span className="font-mono font-medium">v{draft}</span>
            Borrador
          </span>
          <Link
            to={`${paths.panel(processId)}?publicar=1`}
            className="group inline-flex items-center gap-1.5 rounded-full bg-ink py-1 pl-2.5 pr-3 text-[11.5px] font-medium text-on-ink hover:bg-ink/90"
          >
            <Rocket
              size={11}
              strokeWidth={2}
              className="transition-transform duration-200 group-hover:-translate-y-px group-hover:translate-x-px motion-reduce:transition-none"
            />
            Publicar
          </Link>
        </>
      ) : null}
    </div>
  )
}
