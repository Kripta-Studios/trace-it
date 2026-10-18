import { RunCost } from '../components/run/RunCost'
import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, PlayCircle } from 'lucide-react'
import { api } from '../api/client'
import { keys } from '../api/queries'
import { QueueList } from '../components/run/QueueList'
import { TracePane } from '../components/run/TracePane'
import { ProcessScreen } from '../components/process/ProcessScreen'
import { ExportButton } from '../components/process/ExportButton'
import { Input } from '../components/shell/Controls'
import { EmptyState, ErrorNotice, Notice } from '../components/shell/Notice'
import { cn } from '../lib/cn'
import { t } from '../i18n'
import { formatRunDate } from '../lib/format'
import { paths } from '../lib/paths'

export function Instances() {
  const processId = Number(useParams().processId)
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('TODAS')

  const process = useQuery({
    queryKey: keys.process(processId),
    queryFn: () => api.getProcess(processId),
  })
  // The server filters; the summary gives the counts for the menu, which the filter must not change.
  const text = useDeferredValue(search.trim())
  const filters = {
    q: text || undefined,
    status: filter === 'PENDING' ? 'PENDING' : undefined,
    decision: filter !== 'TODAS' && filter !== 'PENDING' ? filter : undefined,
  }
  // With `?run=`, the list is the cases that run decided. The filter lives in the URL,
  // so the browser's Back returns to the Panel.
  const runId = params.get('run') ? Number(params.get('run')) : undefined
  const run = useQuery({
    queryKey: keys.run(runId ?? 0),
    queryFn: () => api.getRun(runId!),
    enabled: Boolean(runId),
  })
  const instances = useQuery({
    queryKey: keys.instances(processId, filters),
    queryFn: () => api.listInstances(processId, filters),
    enabled: !runId,
  })
  const summary = useQuery({
    queryKey: keys.summary(processId),
    queryFn: () => api.summary(processId),
  })
  const versions = useQuery({
    queryKey: keys.versions(processId),
    queryFn: () => api.listVersions(processId),
  })

  const rows = useMemo(() => {
    if (!runId) return instances.data ?? []
    const needle = text.toLowerCase()
    return (run.data?.decisions ?? [])
      .map((item) => ({
        id: item.instance_id,
        name: item.name,
        status: 'DECIDED',
        decision: item.decision,
        review_pending: false,
      }))
      .filter((item) => !needle || item.name.toLowerCase().includes(needle))
      .filter((item) => filter === 'TODAS' || item.decision === filter)
  }, [runId, instances.data, run.data, text, filter])
  const total = runId ? (run.data?.decisions.length ?? 0) : (summary.data?.instances ?? rows.length)
  const select = (id: number) =>
    setParams(runId ? { run: String(runId), i: String(id) } : { i: String(id) })

  const requestedId = params.get('i') ? Number(params.get('i')) : undefined
  const filtered = Boolean(text) || filter !== 'TODAS' || Boolean(runId)
  // A filter must never leave an unrelated document open. Without a filter, honor
  // direct links even if that document is absent from the list, and show its error.
  const selectedId = filtered && !rows.some((item) => item.id === requestedId)
    ? rows[0]?.id
    : requestedId ?? rows[0]?.id

  const detail = useQuery({
    queryKey: keys.instance(selectedId ?? 0),
    queryFn: () => api.getInstance(selectedId!),
    enabled: Boolean(selectedId),
  })
  const trace = useQuery({
    queryKey: keys.trace(selectedId ?? 0),
    queryFn: () => api.getTrace(selectedId!),
    enabled: Boolean(selectedId),
  })

  const outcomes = Object.entries(
    (runId ? run.data?.by_decision : summary.data?.by_decision) ?? {},
  ).sort(([a], [b]) => a.localeCompare(b))
  const pending = runId ? 0 : (summary.data?.by_status.PENDING ?? 0)

  return (
    <ProcessScreen
      processId={processId}
      crumbs={[
        { label: 'Procesos', to: paths.processes },
        { label: process.data?.name ?? '…', to: paths.process(processId) },
        { label: 'Ejecuciones' },
      ]}
      actions={<ExportButton processId={processId} />}
    >

      {instances.isError || run.isError ? (
        <div className="px-4 py-4 sm:px-6">
          <ErrorNotice error={instances.error ?? run.error} />
        </div>
      ) : null}
      {detail.isError || trace.isError ? (
        <div className="px-4 py-4 sm:px-6">
          <ErrorNotice error={detail.error ?? trace.error} />
        </div>
      ) : null}
      {run.data ? (
        <div className="px-4 py-4 sm:px-6">
          <Notice
            title={`Ejecución del ${formatRunDate(run.data.started_at)} · v${run.data.version_number}`}
            action={
              <Link to={paths.panel(processId)} className="text-[12px] text-muted hover:text-ink">
                Volver
              </Link>
            }
          >
            {run.data.decided} decisiones
            <RunCost cost={run.data.cost} detail />
          </Notice>
        </div>
      ) : null}

      {!runId && summary.data?.instances === 0 ? (
        <EmptyState
          icon={PlayCircle}
          title="Aún no hay ejecuciones"
          className="flex-1 justify-center"
          action={
            <Link
              to={paths.panel(processId)}
              className="inline-flex items-center gap-1.5 rounded-full bg-ink px-3.5 py-1.5 text-[12px] font-medium text-on-ink hover:bg-ink/90"
            >
              Ir al Panel
            </Link>
          }
        >
          Ejecuta un lote desde el Panel. Cada documento aparecerá aquí con su decisión y su traza.
        </EmptyState>
      ) : (
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
        <QueueList
          items={rows}
          decisionTypes={process.data?.decision_types}
          total={total}
          selectedId={selectedId}
          onSelect={(item) => select(item.id)}
          header={
            <div className="space-y-1.5">
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Nombre del archivo…"
                className="bg-surface"
              />
              <FilterMenu
                value={filter}
                onChange={setFilter}
                options={[
                  { value: 'TODAS', label: `Todas ${total}` },
                  ...outcomes.map(([key, count]) => ({
                    value: key,
                    label: `${key.replaceAll('_', ' ')} ${count}`,
                  })),
                  ...(pending > 0
                    ? [{ value: 'PENDING', label: `${t('instanceStatus.PENDING')} ${pending}` }]
                    : []),
                ]}
              />
            </div>
          }
        />
        <TracePane
          instance={detail.data}
          trace={trace.data}
          schema={process.data?.symbols}
          versions={versions.data}
          decisionTypes={process.data?.decision_types}
        />
      </div>
      )}
    </ProcessScreen>
  )
}

function FilterMenu({
  value,
  onChange,
  options,
}: {
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const current = options.find((option) => option.value === value)?.label ?? options[0]?.label

  useEffect(() => {
    if (!open) return
    const onPointer = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((next) => !next)}
        className="flex w-full items-center justify-between gap-2 rounded-full bg-surface px-3 py-1.5 text-left ring-1 ring-line"
      >
        <span className="truncate font-mono text-[11px] text-ink">{current}</span>
        <ChevronDown
          size={12}
          strokeWidth={1.75}
          className={cn(
            'shrink-0 text-faint transition-transform duration-150 ease-[cubic-bezier(0.23,1,0.32,1)]',
            open && 'rotate-180',
          )}
        />
      </button>
      {open ? (
        <ul
          role="listbox"
          className="absolute z-20 mt-1 w-full origin-top rounded-[12px] bg-surface p-1 shadow-float ring-1 ring-line"
        >
          {options.map((option) => (
            <li key={option.value}>
              <button
                type="button"
                role="option"
                aria-selected={option.value === value}
                onClick={() => {
                  onChange(option.value)
                  setOpen(false)
                }}
                className={cn(
                  'flex w-full rounded-[8px] px-2.5 py-1.5 text-left font-mono text-[11px]',
                  option.value === value ? 'bg-canvas text-ink' : 'text-muted hover:bg-canvas hover:text-ink',
                )}
              >
                {option.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
