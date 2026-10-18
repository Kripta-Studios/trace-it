import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowUp,
  Check,
  FileUp,
  MessageSquareText,
  Paperclip,
  RefreshCw,
  X,
} from 'lucide-react'
import { api } from '../../api/client'
import type {
  DiscoveryMessage,
  DiscoveryPlan,
  DiscoverySession,
  DiscoverySessionSummary,
  Proposal,
  SpanNode,
  ValidationChange,
} from '../../api/contracts'
import { families, keys } from '../../api/queries'
import { cn } from '../../lib/cn'
import { paths } from '../../lib/paths'
import { Button, Select, Textarea } from '../shell/Controls'
import { EmptyState, ErrorNotice, Notice } from '../shell/Notice'
import { Markdown } from '../shell/Markdown'
import { TerminalLoader } from '../shell/TerminalLoader'
import { NestedCard } from '../shell/Well'
import { FileChip, revokePreview, toPreview, type FilePreview } from './FileChip'
import { ValidationImpact } from './ValidationImpact'
import { RulesPane } from './RulesPane'
import { VersionChip, VersionView } from './ProcessVersions'
import { t } from '../../i18n'

type Attachment = FilePreview & { file: File }

type Submission = {
  message: string
  files: Attachment[]
}

type ReviewItem = {
  key: string
  title: string
  description: string
  evidence: { reference: string; explanation: string }[]
  detail?: string
}

type Preview = {
  valid?: boolean
  examples?: {
    name: string
    expected: string
    actual: string
    reason?: string
    passed: boolean
  }[]
  impact?: {
    unchanged?: number
    changes?: ValidationChange[]
    conflicts?: ValidationChange[]
    resolved_by_person?: (ValidationChange & { resolution: string; resolved_by: string })[]
    errors?: { instance_id: number; name?: string; reason?: string }[]
    error?: string
  }
  compilations?: { proposal?: string; report?: { valid?: boolean }; interpretation?: string }[]
  source_counts?: Record<string, number>
  source_mutations?: unknown
  review?: { valid?: boolean; basis?: string; previews?: unknown[] } | null
}

const CHAT_VERBS = ['leyendo el proceso', 'contrastando la evidencia', 'redactando la respuesta']
const PREPARE_VERBS = [
  'normalizando las reglas',
  'compilando y probando',
  'ejecutando los ejemplos',
  'comparando con el histórico',
]
const TABULAR_EVIDENCE = /\.(xlsx|csv|json)$/i

function messageOf(value: Record<string, unknown>): DiscoveryMessage | null {
  if ((value.role !== 'user' && value.role !== 'assistant') || typeof value.text !== 'string') {
    return null
  }
  if (value.role === 'user' && /^(accepted|rejected):/.test(value.text)) return null
  return {
    role: value.role,
    text: value.text,
    author: typeof value.author === 'string' ? value.author : undefined,
    mode: value.mode === 'discuss' || value.mode === 'revise' ? value.mode : undefined,
    evidence: Array.isArray(value.evidence)
      ? value.evidence.filter((item): item is string => typeof item === 'string')
      : undefined,
    questions: Array.isArray(value.questions)
      ? value.questions.filter((item): item is string => typeof item === 'string')
      : undefined,
    trace_id: typeof value.trace_id === 'string' ? value.trace_id : undefined,
  }
}

function evidenceText(evidence: ReviewItem['evidence']): string {
  return evidence.map((item) => `${item.reference}: ${item.explanation}`).join(' · ')
}

function setupDescription(plan: DiscoveryPlan): string {
  const outcomes = plan.decision_types.map((item) => item.name).join(', ') || 'sin salidas'
  const symbols = plan.symbols.map((item) => item.name).join(', ') || 'sin inputs'
  return `${plan.name || 'Proceso sin nombre'} · ${outcomes} · ${symbols}`
}

/** Every key the backend requires before preparation, in the same order it validates them. */
function reviewItems(plan: DiscoveryPlan): ReviewItem[] {
  return [
    {
      key: 'setup',
      title: 'Definición del proceso',
      description: setupDescription(plan),
      evidence: [],
      detail: plan.description,
    },
    ...plan.connectors.map((item) => ({
      key: `connector:${item.name}`,
      title: `Conector · ${item.name}`,
      description: item.explanation,
      evidence: item.evidence,
      detail: `Campos obligatorios: ${item.required.join(', ')}`,
    })),
    ...plan.sources.map((item) => ({
      key: `source:${item.name}`,
      title: `Fuente · ${item.name}`,
      description: item.explanation,
      evidence: item.evidence,
      detail: `${item.operation} · ${item.kind}`,
    })),
    ...plan.rules.map((item) => ({
      key: `rule:${item.name}`,
      title: item.summary || `Regla · ${item.name}`,
      description: item.text,
      evidence: item.evidence,
      detail: `${item.type} → ${item.decision}`,
    })),
    ...plan.guidance.map((item) => ({
      key: `guidance:${item.name}`,
      title: `Criterio del revisor · ${item.name}`,
      description: item.text,
      evidence: item.evidence,
    })),
    ...plan.examples.map((item) => ({
      key: `example:${item.name}`,
      title: `Ejemplo · ${item.name}`,
      description: `${item.explanation} Resultado esperado: ${item.decision}.`,
      evidence: [],
      detail: JSON.stringify(item.instance),
    })),
  ]
}

function conversations(
  sessions: DiscoverySessionSummary[],
  processId: number | undefined,
): DiscoverySessionSummary[] {
  if (processId == null) {
    return sessions.filter(
      (item) => item.published_process_id == null && item.process_id == null,
    )
  }
  return sessions.filter(
    (item) => item.process_id === processId || item.published_process_id === processId,
  )
}

function conversationLabel(item: DiscoverySessionSummary): string {
  const status = item.published_process_id == null ? 'En curso' : 'Publicada'
  return `${status} · ${item.name || `Conversación ${item.id}`}`
}

function draftIdFromSearch(params: URLSearchParams): number | null {
  const value = params.get('draft')
  if (!value || !/^\d+$/.test(value)) return null
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

function snapshotNames(session: DiscoverySession): Set<string> {
  return new Set(
    session.snapshots.flatMap((value) => {
      const row = value as Record<string, unknown>
      return typeof row.name === 'string' ? [row.name] : []
    }),
  )
}

function changesOf(session: DiscoverySession) {
  return session.changes.flatMap((value) => {
    const row = value as Record<string, unknown>
    return typeof row.field === 'string'
      ? [{ field: row.field, before: row.before, after: row.after }]
      : []
  })
}

const CHANGE_LABELS: Record<string, string> = {
  name: 'Nombre',
  description: 'Contexto y convenciones',
  decision_types: 'Tipos de decisión',
  symbols: 'Inputs',
  connectors: 'Conectores',
  sources: 'Fuentes de verdad',
  rules: 'Reglas',
  decision_review: 'Revisión subjetiva',
  guidance: 'Criterios del revisor',
  examples: 'Ejemplos de aceptación',
}

export function ProcessDraftChat({
  processId,
  processName,
  className,
}: {
  processId?: number
  processName?: string
  className?: string
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [draft, setDraft] = useState('')
  const [files, setFiles] = useState<Attachment[]>([])
  const fileInput = useRef<HTMLInputElement>(null)
  const history = useRef<HTMLDivElement>(null)

  const sessions = useQuery({
    queryKey: keys.discoverySessions,
    queryFn: () => api.listDiscoverySessions(),
  })
  const candidates = useMemo(
    () => conversations(sessions.data ?? [], processId),
    [processId, sessions.data],
  )

  const requestedId = draftIdFromSearch(searchParams)
  const activeId =
    requestedId != null
      ? candidates.some((item) => item.id === requestedId)
        ? requestedId
        : null
      : processId != null
        ? candidates[0]?.id ?? null
        : null

  const session = useQuery({
    queryKey: keys.discoverySession(activeId ?? 0),
    queryFn: () => api.getDiscoverySession(activeId as number),
    enabled: activeId != null,
  })
  const current = session.data
  const versionDraft = useQuery({
    queryKey: keys.execution(processId ?? 0),
    queryFn: () => api.getExecution(processId as number),
    enabled: processId != null,
  })
  const openProposals = useQuery({
    queryKey: keys.proposals(processId ?? 0, 'open'),
    queryFn: () => api.listProposals(processId as number, 'open'),
    enabled: processId != null,
    select: (items) => items.filter((item) => item.channel !== 'escalation'),
  })

  const cache = (next: DiscoverySession) => {
    queryClient.setQueryData(keys.discoverySession(next.id), next)
  }

  const selectDraft = (id: number) => setSearchParams({ draft: String(id) })

  const store = async (next: DiscoverySession) => {
    cache(next)
    await queryClient.invalidateQueries({ queryKey: keys.discoverySessions })
    return next
  }

  const start = useMutation({
    mutationFn: () => api.startDiscoverySession(processId, processName),
    onSuccess: async (next) => {
      await store(next)
      selectDraft(next.id)
    },
  })

  const send = useMutation({
    mutationFn: async (submission: Submission) => {
      let next = current ?? (await api.startDiscoverySession(processId, processName))
      cache(next)
      for (const file of submission.files) {
        if (!TABULAR_EVIDENCE.test(file.name)) continue
        next = await api.uploadDraftEvidence(next.id, next.revision, file.file)
        cache(next)
      }
      next = await api.messageDiscoverySession(
        next.id,
        next.revision,
        submission.message,
        'revise',
      )
      return next
    },
    onMutate: (submission) => {
      setDraft('')
      setFiles([])
      return submission
    },
    onError: (_error, submission) => {
      setDraft((value) => value || submission.message)
      setFiles((value) => [...submission.files, ...value])
    },
    onSuccess: async (next, submission) => {
      submission.files.forEach(revokePreview)
      await store(next)
      selectDraft(next.id)
      if (processId != null) {
        await queryClient.invalidateQueries({ queryKey: keys.proposals(processId, 'open') })
      }
    },
  })

  const review = useMutation({
    mutationFn: async ({
      keys: proposalKeys,
      disposition,
    }: {
      keys: string[]
      disposition: 'accepted' | 'rejected'
    }) => {
      if (!current) throw new Error('No hay conversación abierta')
      let next = current
      for (const proposal of proposalKeys) {
        next = await api.reviewDiscoveryProposal(
          next.id,
          next.revision,
          proposal,
          disposition,
          disposition === 'rejected' ? 'Necesita cambios; la explicación sigue en el chat.' : '',
        )
      }
      return next
    },
    onSuccess: store,
  })

  const syncSource = useMutation({
    mutationFn: (name: string) => {
      if (!current) throw new Error('No hay conversación abierta')
      return api.syncDiscoverySource(current.id, current.revision, name)
    },
    onSuccess: store,
  })

  const prepare = useMutation({
    mutationFn: () => {
      if (!current) throw new Error('No hay conversación abierta')
      return api.prepareDiscoverySession(current.id, current.revision)
    },
    onSuccess: store,
  })

  const publish = useMutation({
    mutationFn: () => {
      if (!current) throw new Error('No hay conversación abierta')
      return api.publishDiscoverySession(current.id, current.revision)
    },
    onSuccess: async (next) => {
      await store(next)
      await queryClient.invalidateQueries()
      if (next.published_process_id != null) navigate(paths.process(next.published_process_id))
    },
  })

  const messages = (current?.messages ?? []).flatMap((value, index, values) => {
    const message = messageOf(value)
    if (
      message?.role === 'assistant' &&
      !message.trace_id &&
      current?.trace_id &&
      !values.slice(index + 1).some((later) => later.role === 'assistant')
    ) {
      message.trace_id = current.trace_id
    }
    return message ? [message] : []
  })
  const visibleMessages = send.isPending && send.variables
    ? [...messages, { role: 'user' as const, text: send.variables.message }]
    : messages
  const items = current ? reviewItems(current.plan) : []
  const pending = items.filter((item) => current?.reviews[item.key] !== 'accepted')
  // A question is sent through the authoring endpoint so it can stay in this conversation, but
  // it is not a revision unless the saved configuration actually differs from the baseline.
  const hasRevision = current != null && changesOf(current).length > 0
  const questions = [...new Set(current?.plan.questions ?? [])]
  const allAccepted = items.length > 0 && pending.length === 0
  const missingPlan = current
    ? [
        !current.plan.name.trim() ? 'un nombre' : null,
        current.plan.decision_types.length === 0 ? 'tipos de decisión' : null,
        current.plan.examples.length === 0 ? 'ejemplos de aceptación' : null,
      ].filter((item): item is string => item != null)
    : []
  const blockedByVersionDraft = versionDraft.data?.revision != null
  const canPrepare =
    hasRevision &&
    allAccepted &&
    questions.length === 0 &&
    missingPlan.length === 0 &&
    !blockedByVersionDraft
  const preview = current?.preview as Preview | null | undefined
  const busy = send.isPending || review.isPending || syncSource.isPending || prepare.isPending
  const error =
    sessions.error ??
    session.error ??
    versionDraft.error ??
    openProposals.error ??
    start.error ??
    send.error ??
    review.error ??
    syncSource.error ??
    prepare.error ??
    publish.error

  useEffect(() => {
    history.current?.scrollTo({ top: history.current.scrollHeight, behavior: 'smooth' })
  }, [visibleMessages.length, send.isPending])

  const submit = () => {
    if (send.isPending || (!draft.trim() && files.length === 0)) return
    send.mutate({
      message: draft.trim() || 'Revisa la evidencia adjunta y propón los cambios necesarios.',
      files,
    })
  }

  const addFiles = (incoming: File[]) => {
    setFiles((existing) => [...existing, ...incoming.map(toPreview)])
  }
  const removeFile = (id: string) => {
    setFiles((existing) => {
      const removed = existing.find((item) => item.id === id)
      if (removed) revokePreview(removed)
      return existing.filter((item) => item.id !== id)
    })
  }

  if (!current && (sessions.isPending || (activeId != null && session.isPending))) {
    return <TerminalLoader verbs={['abriendo la conversación']} className="p-8" />
  }

  if (!current) {
    return (
      <div
        className={cn(
          'grid min-h-0 flex-1',
          processId != null && 'lg:grid-cols-[minmax(0,1.15fr)_minmax(21rem,0.85fr)]',
          className,
        )}
      >
        <div className="min-h-0 overflow-y-auto px-4 py-6 sm:px-6">
          {error ? <ErrorNotice error={error} /> : null}
          {openProposals.data?.length ? <PendingProposals proposals={openProposals.data} /> : null}
          <EmptyState
            icon={MessageSquareText}
            title={processId == null ? 'Crea el proceso hablando' : 'Revisa el proceso hablando'}
            action={
              <Button tone="primary" disabled={start.isPending} onClick={() => start.mutate()}>
                {start.isPending ? 'Abriendo…' : 'Empezar conversación'}
              </Button>
            }
          >
            Describe qué debe decidir, adjunta sus fuentes y responde las preguntas. Nada se aplica
            hasta que revises el resultado y pulses Publicar.
          </EmptyState>
          {processId == null && candidates.length ? (
            <div className="mx-auto max-w-xl px-2 pb-2">
              <NestedCard label="borradores guardados">
                <ul className="divide-y divide-hairline">
                  {candidates.map((item) => (
                    <li key={item.id}>
                      <Link
                        to={paths.newProcessDraft(item.id)}
                        className="flex items-center justify-between gap-3 px-3.5 py-3 hover:bg-well"
                      >
                        <span className="min-w-0">
                          <span className="block truncate text-[13px] text-ink">
                            {item.name || `Borrador ${item.id}`}
                          </span>
                          <span className="mt-0.5 block font-mono text-[10px] text-faint">
                            conversación {item.id} · revisión {item.revision}
                          </span>
                        </span>
                        <span className="shrink-0 text-[12px] text-muted">Continuar</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              </NestedCard>
            </div>
          ) : null}
        </div>
        {processId != null ? (
          <aside className="min-h-0 overflow-y-auto border-t border-hairline px-5 py-5 lg:border-l lg:border-t-0">
            <ProcessRules processId={processId} />
          </aside>
        ) : null}
      </div>
    )
  }

  if (current.published_process_id != null) {
    return (
      <div className={cn('flex min-h-0 flex-1 flex-col', className)}>
        <header className="flex items-center justify-between gap-3 border-b border-hairline px-5 py-3">
          <div className="min-w-0">
            <p className="truncate text-[13px] font-medium text-ink">
              {current.plan.name || processName || 'Proceso publicado'}
            </p>
            <p className="font-mono text-[10px] text-faint">
              conversación {current.id} · publicada · solo lectura
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Select
              aria-label="Historial de conversaciones"
              value={current.id}
              onChange={(event) => selectDraft(Number(event.target.value))}
              className="w-56 py-1"
            >
              {candidates.map((item) => (
                <option key={item.id} value={item.id}>
                  {conversationLabel(item)}
                </option>
              ))}
            </Select>
            <Button tone="ghost" disabled={start.isPending} onClick={() => start.mutate()}>
              Nueva
            </Button>
          </div>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          <Notice title="Conversación publicada">
            Este es el chat que creó o modificó el proceso. Se conserva como historial y ya no se
            puede editar.
          </Notice>
          <ol className="mx-auto mt-5 max-w-4xl space-y-4">
            {messages.map((message, index) => (
              <li
                key={`${index}:${message.text}`}
                className={cn('flex', message.role === 'user' ? 'justify-end' : 'justify-start')}
              >
                <ProcessDraftMessage message={message} />
              </li>
            ))}
          </ol>
          {error ? <div className="mx-auto mt-4 max-w-4xl"><ErrorNotice error={error} /></div> : null}
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'grid min-h-0 min-w-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1.15fr)_minmax(21rem,0.85fr)]',
        className,
      )}
    >
      <section className="flex min-h-0 min-w-0 flex-col border-b border-hairline lg:border-b-0 lg:border-r">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-5 py-3">
          <div className="min-w-0">
            <p className="truncate text-[13px] font-medium text-ink">
              {current.plan.name || processName || 'Nuevo proceso'}
            </p>
            <p className="font-mono text-[10px] text-faint">
              conversación {current.id} · revisión {current.revision}
            </p>
          </div>
          <div className="flex min-w-0 max-w-full items-center gap-2">
            {candidates.length ? (
              <Select
                aria-label="Historial de conversaciones"
                value={current.id}
                onChange={(event) => selectDraft(Number(event.target.value))}
                className="min-w-0 w-56 py-1"
              >
                {candidates.map((item) => (
                  <option key={item.id} value={item.id}>
                    {conversationLabel(item)}
                  </option>
                ))}
              </Select>
            ) : null}
            <Button tone="ghost" disabled={start.isPending} onClick={() => start.mutate()}>
              Nueva
            </Button>
          </div>
        </header>

        <div ref={history} className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          {visibleMessages.length === 0 ? (
            <EmptyState title="Cuéntame qué tiene que decidir">
              Pregunta algo o pide cambios. El mismo chat puede revisar el contexto, los inputs,
              las salidas, las fuentes, los conectores, las reglas y los criterios del revisor.
              Puedes pegar la política o adjuntar XLSX, CSV y JSON como evidencia.
            </EmptyState>
          ) : (
            <ol className="space-y-4">
              {visibleMessages.map((message, index) => (
                <li
                  key={`${index}:${message.text}`}
                  className={cn('flex', message.role === 'user' ? 'justify-end' : 'justify-start')}
                >
                  <ProcessDraftMessage message={message} />
                </li>
              ))}
            </ol>
          )}
          {send.isPending ? <TerminalLoader verbs={CHAT_VERBS} className="mt-4" /> : null}
          {prepare.isPending ? <TerminalLoader verbs={PREPARE_VERBS} className="mt-4" /> : null}
          {error ? <div className="mt-4"><ErrorNotice error={error} /></div> : null}
        </div>

        <div className="shrink-0 border-t border-hairline px-5 py-4">
          {files.length ? (
            <ul className="mb-2 flex flex-wrap gap-2">
              {files.map((file) => (
                <li key={file.id}>
                  <FileChip file={file} onRemove={() => removeFile(file.id)} />
                </li>
              ))}
            </ul>
          ) : null}
          <Textarea
            rows={3}
            value={draft}
            disabled={review.isPending || syncSource.isPending || prepare.isPending}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                event.preventDefault()
                submit()
              }
            }}
            placeholder="Pregunta algo o pide un cambio en el proceso."
          />
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <button
                type="button"
                title="Adjuntar evidencia"
                onClick={() => fileInput.current?.click()}
                className="grid h-8 w-8 place-items-center rounded-full text-muted hover:bg-canvas hover:text-ink"
              >
                <Paperclip size={14} strokeWidth={1.6} />
              </button>
            </div>
            <Button
              tone="primary"
              disabled={busy || (!draft.trim() && files.length === 0)}
              onClick={submit}
            >
              <ArrowUp size={13} strokeWidth={2} />
              Enviar
            </Button>
          </div>
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            accept=".xlsx,.csv,.json"
            onChange={(event) => {
              addFiles([...(event.target.files ?? [])])
              event.target.value = ''
            }}
          />
        </div>
      </section>

      <aside className="min-h-0 overflow-y-auto px-5 py-5">
        <div className="space-y-5">
          {processId != null ? (
            <p className="text-[12px] text-muted">
              Este chat propone cualquier cambio de definición. Nada se publica sin revisar y
              probarlo. El{' '}
              <Link to={paths.definitionManual(processId)} className="underline hover:text-ink">
                editor manual
              </Link>{' '}
              sigue disponible para cambios puntuales.
            </p>
          ) : null}

          {processId != null ? <ProcessRules processId={processId} /> : null}

          {openProposals.data?.length ? <PendingProposals proposals={openProposals.data} /> : null}

          {processId != null && blockedByVersionDraft ? (
            <Notice
              tone="warning"
              title="Hay otro borrador abierto"
              action={
                <Link to={`${paths.panel(processId)}?publicar=1`}>
                  <Button>Revisarlo</Button>
                </Link>
              }
            >
              Publícalo o descártalo antes de preparar esta conversación. El chat no lo sobrescribe.
            </Notice>
          ) : null}

          {questions.length ? (
            <Notice tone="warning" title={`${questions.length} pregunta${questions.length === 1 ? '' : 's'} pendiente${questions.length === 1 ? '' : 's'}`}>
              <ul className="space-y-2">
                {questions.map((question) => (
                  <li key={question}>
                    <button
                      type="button"
                      className="text-left hover:text-ink"
                      onClick={() => {
                        setDraft(`Sobre "${question}": `)
                      }}
                    >
                      {question}
                    </button>
                  </li>
                ))}
              </ul>
            </Notice>
          ) : null}

          {hasRevision && changesOf(current).length ? (
            <NestedCard label="cambios propuestos">
              <ul className="divide-y divide-hairline">
                {changesOf(current).map((change) => (
                  <li key={change.field} className="px-3.5 py-2.5">
                    <details>
                      <summary className="cursor-pointer text-[13px] text-ink">
                        {CHANGE_LABELS[change.field] ?? change.field}
                      </summary>
                      <div className="mt-2 grid gap-2 text-[11px] text-muted sm:grid-cols-2">
                        <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-[8px] bg-canvas p-2">
                          {JSON.stringify(change.before, null, 2)}
                        </pre>
                        <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-[8px] bg-canvas p-2">
                          {JSON.stringify(change.after, null, 2)}
                        </pre>
                      </div>
                    </details>
                  </li>
                ))}
              </ul>
            </NestedCard>
          ) : null}

          {hasRevision ? (
            <section className="space-y-2">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h2 className="text-[15px] font-medium text-ink">Revisión</h2>
                  <p className="text-[11px] text-faint">
                    {items.length - pending.length} de {items.length} aprobadas
                  </p>
                </div>
                {pending.length ? (
                  <Button
                    tone="primary"
                    disabled={review.isPending}
                    onClick={() =>
                      review.mutate({ keys: pending.map((item) => item.key), disposition: 'accepted' })
                    }
                  >
                    <Check size={12} />
                    Aprobar pendientes
                  </Button>
                ) : null}
              </div>
              <ul className="space-y-2">
                {items.map((item) => (
                  <ProcessDraftReviewItem
                    key={item.key}
                    item={item}
                    disposition={current.reviews[item.key]}
                    reviewing={review.isPending}
                    onReview={(disposition) => review.mutate({ keys: [item.key], disposition })}
                  />
                ))}
              </ul>
            </section>
          ) : null}

          {current.plan.connectors.length ? (
            <section className="space-y-2">
              <h2 className="text-[15px] font-medium text-ink">Conectores</h2>
              {current.plan.connectors.map((connector) => {
                const loaded = snapshotNames(current).has(connector.name)
                const accepted = current.reviews[`connector:${connector.name}`] === 'accepted'
                return (
                  <div key={connector.name} className="flex items-center gap-3 rounded-[12px] bg-canvas px-3 py-2 ring-1 ring-line">
                    <div className="min-w-0 flex-1">
                      <p className="font-mono text-[11px] text-ink">{connector.name}</p>
                      <p className="text-[11px] text-muted">{loaded ? 'Snapshot cargado' : 'Sin snapshot'}</p>
                    </div>
                    <Button
                      disabled={!accepted || loaded || syncSource.isPending}
                      onClick={() => syncSource.mutate(connector.name)}
                    >
                      <RefreshCw size={11} />
                      Sincronizar
                    </Button>
                  </div>
                )
              })}
            </section>
          ) : null}

          {hasRevision && !preview ? (
            <NestedCard label="preparar versión">
              <div className="space-y-3 px-3.5 py-3">
                <p className="text-[12px] leading-5 text-muted">
                  Compila las reglas, ejecuta los ejemplos y compara el resultado con las decisiones anteriores.
                </p>
                {!allAccepted ? <p className="text-[11px] text-escalar">Aprueba todas las propuestas antes de preparar.</p> : null}
                {questions.length ? <p className="text-[11px] text-escalar">Responde las preguntas pendientes antes de preparar.</p> : null}
                {missingPlan.length ? <p className="text-[11px] text-escalar">Faltan {missingPlan.join(', ')}.</p> : null}
                {blockedByVersionDraft ? <p className="text-[11px] text-escalar">Cierra el otro borrador antes de preparar.</p> : null}
                <Button
                  tone="primary"
                  disabled={!canPrepare || prepare.isPending}
                  onClick={() => prepare.mutate()}
                >
                  <FileUp size={12} />
                  {prepare.isPending ? 'Preparando…' : 'Preparar y comprobar'}
                </Button>
              </div>
            </NestedCard>
          ) : null}

          {preview ? (
            <ProcessDraftPreview
              processId={processId}
              preview={preview}
              publishing={publish.isPending}
              onPrepare={() => prepare.mutate()}
              onPublish={() => publish.mutate()}
            />
          ) : null}
        </div>
      </aside>
    </div>
  )
}

/** The same rule workspace as the manual editor, independent of the chat's proposals. */
function ProcessRules({ processId }: { processId: number }) {
  const process = useQuery({
    queryKey: keys.process(processId),
    queryFn: () => api.getProcess(processId),
  })
  const rules = useQuery({
    queryKey: keys.rules(processId),
    queryFn: () => api.listRules(processId),
    refetchInterval: (query) =>
      query.state.data?.some((rule) => rule.status === 'compiling') ? 2_000 : false,
  })
  const versions = useQuery({
    queryKey: keys.versions(processId),
    queryFn: () => api.listVersions(processId),
  })
  const findings = useQuery({
    queryKey: keys.findings(processId),
    queryFn: () => api.listFindings(processId),
  })
  const history = [...(versions.data ?? [])].sort((a, b) => b.number - a.number)
  const latestVersion = history[0]
  const [viewing, setViewing] = useState<number | null>(null)
  const viewed = history.find((version) => version.id === viewing)
  const error = rules.error ?? process.error ?? versions.error ?? findings.error

  return (
    <section aria-label="Process rules">
      <div className="mb-3 flex justify-end">
        <VersionChip
          versions={history}
          selected={viewed ?? latestVersion}
          onSelect={(version) => setViewing(version.id)}
          findings={findings.data ?? []}
        />
      </div>
      {error ? (
        <ErrorNotice error={error} />
      ) : rules.isPending || process.isPending ? (
        <p className="text-[12px] text-muted">{t('common.loading')}</p>
      ) : viewed && latestVersion ? (
        <VersionView
          key={viewed.id}
          processId={processId}
          version={viewed}
          current={latestVersion}
          rules={rules.data ?? []}
          onBack={() => setViewing(null)}
        />
      ) : (
        <RulesPane
          processId={processId}
          rules={rules.data ?? []}
          outcomes={process.data?.decision_types.map((outcome) => outcome.name) ?? []}
        />
      )}
    </section>
  )
}

function ThinkingTrace({ traceId }: { traceId: string }) {
  const trace = useQuery({
    queryKey: ['trace', traceId],
    queryFn: () => api.getAuditTrace(traceId),
  })

  return (
    <details className="mt-3 border-t border-current/10 pt-2">
      <summary className="cursor-pointer font-mono text-[10px] text-faint">
        Traza de razonamiento
      </summary>
      {trace.isPending ? (
        <p className="mt-2 text-[11px] text-faint">Cargando traza…</p>
      ) : trace.error ? (
        <p className="mt-2 text-[11px] text-nopagar">No se pudo cargar la traza.</p>
      ) : (
        <div className="mt-2 space-y-2">
          {trace.data?.map((span) => <ThinkingSpan key={span.span_id} span={span} />)}
        </div>
      )}
    </details>
  )
}

function ThinkingSpan({ span }: { span: SpanNode }) {
  return (
    <details className="rounded-[8px] bg-canvas px-2.5 py-2 ring-1 ring-line">
      <summary className="cursor-pointer font-mono text-[10px] text-muted">
        {span.step} · {span.status} · {span.duration_ms == null ? '—' : `${span.duration_ms} ms`}
      </summary>
      {span.data ? (
        <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] leading-4 text-ink">
          {JSON.stringify(span.data, null, 2)}
        </pre>
      ) : null}
      {span.children.length ? (
        <div className="mt-2 space-y-2 border-l border-line pl-2">
          {span.children.map((child) => <ThinkingSpan key={child.span_id} span={child} />)}
        </div>
      ) : null}
    </details>
  )
}

function PendingProposals({ proposals }: { proposals: Proposal[] }) {
  return (
    <section className="mb-5 space-y-2">
      <div>
        <h2 className="text-[15px] font-medium text-ink">Propuestas pendientes</h2>
        <p className="text-[11px] text-faint">
          También puedes resolver aquí propuestas del chat y del aprendizaje.
        </p>
      </div>
      <ul className="space-y-2">
        {proposals.map((proposal) => (
          <ProcessProposalCard key={proposal.id} proposal={proposal} />
        ))}
      </ul>
    </section>
  )
}

function ProcessProposalCard({ proposal }: { proposal: Proposal }) {
  const queryClient = useQueryClient()
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')

  const settle = useMutation({
    mutationFn: (accept: boolean) =>
      accept ? api.acceptProposal(proposal.id) : api.rejectProposal(proposal.id, reason.trim()),
    onSuccess: async () => {
      for (const name of families.proposals) {
        await queryClient.invalidateQueries({ queryKey: [name] })
      }
      await queryClient.invalidateQueries({ queryKey: ['process-draft'] })
    },
  })
  const settled = settle.data?.status

  return (
    <li className="rounded-[14px] bg-surface px-3.5 py-3 ring-1 ring-line">
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-[10px] tracking-[0.1em] text-faint">
          {t(`proposalKind.${proposal.kind}`)} · {t(`proposalChannel.${proposal.channel}`)}
        </p>
        {settled ? (
          <span className={cn('text-[11px]', settled === 'accepted' ? 'text-pagar' : 'text-muted')}>
            {settled === 'accepted' ? 'Aceptada' : 'Rechazada'}
          </span>
        ) : (
          <div className="flex gap-1.5">
            <Button
              tone="soft"
              disabled={settle.isPending || (rejecting && !reason.trim())}
              onClick={() => (rejecting ? settle.mutate(false) : setRejecting(true))}
            >
              Rechazar
            </Button>
            <Button tone="primary" disabled={settle.isPending} onClick={() => settle.mutate(true)}>
              Aceptar
            </Button>
          </div>
        )}
      </div>
      <p className="mt-2 text-[12px] leading-5 text-ink">{proposal.summary}</p>
      {proposal.rationale ? <p className="mt-1 text-[11px] text-muted">{proposal.rationale}</p> : null}
      {proposal.evidence.length ? (
        <p className="mt-1 font-mono text-[10px] text-faint">{proposal.evidence.join(' · ')}</p>
      ) : null}
      {rejecting && !settled ? (
        <Textarea
          rows={2}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Por qué no"
          className="mt-2"
        />
      ) : null}
      {settle.isError ? <div className="mt-2"><ErrorNotice error={settle.error} /></div> : null}
    </li>
  )
}

export function ProcessDraftPreview({
  processId,
  preview,
  publishing,
  onPrepare,
  onPublish,
}: {
  processId?: number
  preview: Preview
  publishing: boolean
  onPrepare: () => void
  onPublish: () => void
}) {
  const examples = preview.examples ?? []
  const validCompilations = (preview.compilations ?? []).filter((item) => item.report?.valid).length

  return (
    <NestedCard label="vista previa">
      <div className="space-y-4 px-3.5 py-3">
        <Notice
          tone={preview.valid ? 'neutral' : 'error'}
          title={preview.valid ? 'La versión está lista para publicar' : 'La versión todavía no pasa las comprobaciones'}
        >
          {validCompilations} reglas compiladas · {examples.filter((item) => item.passed).length} de{' '}
          {examples.length} ejemplos correctos
        </Notice>

        {examples.length ? (
          <ul className="divide-y divide-hairline rounded-[10px] bg-canvas px-3 ring-1 ring-line">
            {examples.map((example) => (
              <li key={example.name} className="flex items-start gap-2 py-2 text-[12px]">
                <span className={example.passed ? 'text-pagar' : 'text-nopagar'}>
                  {example.passed ? '✓' : '×'}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="text-ink">{example.name}</span>
                  <span className="block text-[10px] text-muted">
                    esperado {example.expected}, obtenido {example.actual}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        ) : null}

        {processId != null && preview.impact ? (
          <ValidationImpact processId={processId} validation={preview.impact} />
        ) : null}

        {preview.source_counts && Object.keys(preview.source_counts).length ? (
          <p className="text-[11px] text-muted">
            Fuentes: {Object.entries(preview.source_counts).map(([name, count]) => `${name} ${count}`).join(' · ')}
          </p>
        ) : null}
        {preview.review?.basis ? <p className="text-[11px] text-muted">{preview.review.basis}</p> : null}

        <div className="flex flex-wrap gap-2 border-t border-hairline pt-3">
          <Button disabled={publishing} onClick={onPrepare}>
            <RefreshCw size={11} />
            Repetir comprobaciones
          </Button>
          <Button tone="primary" disabled={!preview.valid || publishing} onClick={onPublish}>
            {publishing ? 'Publicando…' : 'Publicar'}
          </Button>
        </div>
        <p className="text-[10px] text-faint">
          Publicar es la aprobación explícita del responsable. El asistente no puede hacerlo por ti.
        </p>
      </div>
    </NestedCard>
  )
}

/** The same conversation message in the editor and the animated product walkthrough. */
export function ProcessDraftMessage({ message }: { message: DiscoveryMessage }) {
  return (
    <div
      className={cn(
        'max-w-[88%] rounded-[16px] px-4 py-3 ring-1',
        message.role === 'user' ? 'bg-ink text-on-ink ring-ink' : 'bg-surface text-ink ring-line',
      )}
    >
      {message.role === 'assistant' ? (
        <Markdown>{message.text}</Markdown>
      ) : (
        <p className="whitespace-pre-line text-[13px] leading-6">{message.text}</p>
      )}
      {message.evidence?.length ? (
        <p
          className={cn(
            'mt-2 font-mono text-[10px]',
            message.role === 'user' ? 'text-white/55' : 'text-faint',
          )}
        >
          {message.evidence.join(' · ')}
        </p>
      ) : null}
      {message.questions?.length ? (
        <ul className="mt-3 space-y-1 border-t border-current/10 pt-2 text-[12px]">
          {message.questions.map((question) => (
            <li key={question}>{question}</li>
          ))}
        </ul>
      ) : null}
      {message.role === 'assistant' && message.trace_id ? (
        <ThinkingTrace traceId={message.trace_id} />
      ) : null}
    </div>
  )
}

/** Shared review row. Approving a demo row cannot invoke the editor's mutations. */
export function ProcessDraftReviewItem({
  item,
  disposition,
  reviewing,
  onReview,
}: {
  item: ReviewItem
  disposition?: string
  reviewing: boolean
  onReview: (disposition: 'accepted' | 'rejected') => void
}) {
  return (
    <li className="rounded-[14px] bg-surface px-3.5 py-3 ring-1 ring-line">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-medium text-ink">{item.title}</p>
          <p className="mt-1 text-[12px] leading-5 text-muted">{item.description}</p>
          {item.detail ? (
            <p className="mt-1 font-mono text-[10px] text-faint">{item.detail}</p>
          ) : null}
          {item.evidence.length ? (
            <p className="mt-1 text-[10px] text-faint">{evidenceText(item.evidence)}</p>
          ) : null}
        </div>
        {disposition === 'accepted' ? (
          <span className="shrink-0 text-[11px] text-pagar">Aprobada</span>
        ) : disposition === 'rejected' ? (
          <span className="shrink-0 text-[11px] text-nopagar">Rechazada</span>
        ) : (
          <div className="flex shrink-0 gap-1">
            <Button
              tone="ghost"
              className="px-2"
              aria-label={`Rechazar ${item.title}`}
              disabled={reviewing}
              onClick={() => onReview('rejected')}
            >
              <X size={11} />
            </Button>
            <Button
              className="px-2"
              aria-label={`Aprobar ${item.title}`}
              disabled={reviewing}
              onClick={() => onReview('accepted')}
            >
              <Check size={11} />
            </Button>
          </div>
        )}
      </div>
    </li>
  )
}
