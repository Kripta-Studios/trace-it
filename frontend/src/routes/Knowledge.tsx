import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Download, FileText, Search, X } from 'lucide-react'
import { api } from '../api/client'
import { get, getBlob, query as queryString } from '../api/http'
import { keys } from '../api/queries'
import type { DocumentEvidence } from '../api/contracts'
import { ProcessScreen } from '../components/process/ProcessScreen'
import { PdfEvidence } from '../components/run/PdfEvidence'
import { Button, Input } from '../components/shell/Controls'
import { EmptyState, ErrorNotice, Notice } from '../components/shell/Notice'
import { cn } from '../lib/cn'
import { formatRunDate } from '../lib/format'
import { paths } from '../lib/paths'

type SourceDocument = {
  instance_id: number
  name: string
  status: string
  uploaded_at: string
  has_text: boolean
  has_evidence: boolean
  excerpt: string
}
type DocumentPage = { items: SourceDocument[]; next_cursor: number | null; query: string }

/** Existing process files, their persisted readings and the original evidence in one workspace. */
export function Knowledge() {
  const processId = Number(useParams().processId)
  const [params] = useSearchParams()
  const process = useQuery({
    queryKey: keys.process(processId),
    queryFn: () => api.getProcess(processId),
  })
  return (
    <ProcessScreen
      processId={processId}
      crumbs={[
        { label: 'Procesos', to: paths.processes },
        { label: process.data?.name ?? '…', to: paths.process(processId) },
        { label: 'Documentación' },
      ]}
      actions={<Link to={paths.process(processId)} className="text-[13px] text-muted hover:text-ink">Ir a la bandeja</Link>}
    >
      <DocumentBrowser key={`${processId}:${params.get('q') ?? ''}`} processId={processId} />
    </ProcessScreen>
  )
}

function DocumentBrowser({ processId }: { processId: number }) {
  const [params, setParams] = useSearchParams()
  const query = (params.get('q') ?? '').slice(0, 300)
  const [draft, setDraft] = useState(query)
  const [selected, setSelected] = useState<SourceDocument | null>(null)
  const documents = useInfiniteQuery({
    queryKey: ['knowledge', processId, query],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => get<DocumentPage>(
      `/processes/${processId}/knowledge${queryString({ query, cursor: pageParam, limit: 25 })}`,
    ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  })
  const items = documents.data?.pages.flatMap((page) => page.items) ?? []
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-b border-hairline px-4 py-4 sm:px-6">
        <h1 className="text-[18px] font-medium tracking-[-0.03em]">Documentación del proceso</h1>
        <p className="mt-1 text-[13px] text-muted">Busca en nombres y texto extraído. Cada resultado conserva su documento original.</p>
        <form
          role="search"
          className="mt-3 flex max-w-2xl gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            setSelected(null)
            setParams(draft.trim() ? { q: draft.trim() } : {})
          }}
        >
          <Input
            aria-label="Buscar documentación"
            placeholder="Nombre, referencia o frase del documento…"
            maxLength={300}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button type="submit"><Search size={14} /> Buscar</Button>
          {query ? <Button type="button" onClick={() => { setDraft(''); setParams({}); setSelected(null) }}>Limpiar</Button> : null}
        </form>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-auto lg:flex-row lg:overflow-hidden">
        <section aria-label="Documentos encontrados" className={cn('min-h-0 overflow-y-auto p-4 sm:p-6', selected ? 'lg:w-[38%] lg:shrink-0' : 'w-full')}>
          {documents.isPending ? <Notice title="Cargando documentación…" /> : null}
          {documents.isError ? <ErrorNotice error={documents.error} action={<Button onClick={() => void documents.refetch()}>Reintentar</Button>} /> : null}
          {!documents.isPending && !documents.isError && items.length === 0 ? (
            <EmptyState icon={FileText} title={query ? 'No se encontraron documentos' : 'Todavía no hay documentos'}>
              {query ? 'Prueba otra referencia o una frase más corta. Los documentos sin texto extraído se encuentran por su nombre.' : 'Los archivos que incorpores desde la bandeja aparecerán aquí con su evidencia.'}
            </EmptyState>
          ) : null}
          {items.length > 0 ? <p aria-live="polite" className="mb-3 text-[12px] text-muted">{items.length} documentos{documents.hasNextPage ? ' · hay más resultados' : ''}{query ? ` · búsqueda: ${query}` : ''}</p> : null}
          <div className={cn('grid gap-3', !selected && 'xl:grid-cols-2')}>
            {items.map((item) => (
              <button
                key={item.instance_id}
                onClick={() => setSelected(item)}
                aria-pressed={selected?.instance_id === item.instance_id}
                className={cn('min-w-0 rounded-[14px] p-4 text-left ring-1 hover:bg-well focus-visible:outline-2 focus-visible:outline-offset-2', selected?.instance_id === item.instance_id ? 'bg-well ring-ink/40' : 'bg-surface ring-line')}
              >
                <span className="flex items-start gap-2 text-[13px] font-medium"><FileText size={16} className="mt-0.5 shrink-0 text-muted" /><span className="break-all">{item.name}</span></span>
                <span className="mt-1 block text-[11px] text-muted">{formatRunDate(item.uploaded_at)} · {item.status === 'DECIDED' ? 'Resuelto' : 'Pendiente'}</span>
                <span className="mt-3 line-clamp-4 block whitespace-pre-wrap break-words text-[12px] leading-5 text-muted">
                  {item.has_text ? excerptText(item.excerpt) : 'Sin texto extraído. Puedes consultar el archivo original.'}
                </span>
                <span className="mt-3 block text-[12px] font-medium">Ver documento y evidencia →</span>
              </button>
            ))}
          </div>
          {documents.hasNextPage ? <div className="mt-4"><Button disabled={documents.isFetchingNextPage} onClick={() => void documents.fetchNextPage()}>{documents.isFetchingNextPage ? 'Cargando…' : 'Mostrar más documentos'}</Button></div> : null}
        </section>
        {selected ? <DocumentPreview key={selected.instance_id} processId={processId} document={selected} onClose={() => setSelected(null)} /> : null}
      </div>
    </div>
  )
}

function DocumentPreview({ processId, document: source, onClose }: { processId: number; document: SourceDocument; onClose: () => void }) {
  const evidence = useQuery({
    queryKey: keys.document(source.instance_id),
    queryFn: () => get<DocumentEvidence>(`/instances/${source.instance_id}/document`),
    enabled: source.has_evidence,
  })
  const [downloadError, setDownloadError] = useState<unknown>(null)
  const [downloading, setDownloading] = useState(false)
  async function download() {
    setDownloadError(null)
    setDownloading(true)
    try {
      const blob = await getBlob(`/instances/${source.instance_id}/file`)
      const url = URL.createObjectURL(blob)
      const anchor = window.document.createElement('a')
      anchor.href = url
      anchor.download = source.name
      anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      setDownloadError(error)
    } finally {
      setDownloading(false)
    }
  }
  return (
    <section aria-label={`Evidencia de ${source.name}`} className="flex min-h-[32rem] min-w-0 flex-1 flex-col border-t border-hairline lg:min-h-0 lg:border-t-0 lg:border-l">
      <div className="flex flex-wrap items-center gap-3 border-b border-hairline px-4 py-3">
        <p className="min-w-0 flex-1 break-all text-[13px] font-medium">{source.name}</p>
        <Link to={paths.instance(processId, source.instance_id)} className="text-[12px] underline underline-offset-4">Abrir caso</Link>
        <button aria-label="Cerrar documento" onClick={onClose} className="rounded p-1 hover:bg-well"><X size={16} /></button>
      </div>
      <div className="flex items-center justify-between gap-3 border-b border-hairline px-4 py-2">
        <p className="text-[11px] text-muted">Fuente original · la búsqueda no modifica decisiones.</p>
        <Button disabled={downloading} onClick={() => void download()}><Download size={13} /> {downloading ? 'Descargando…' : 'Descargar'}</Button>
      </div>
      {downloadError ? <div className="p-3"><ErrorNotice error={downloadError} /></div> : null}
      {source.has_evidence && evidence.isPending ? <div className="p-4"><Notice title="Cargando evidencia…" /></div> : null}
      {evidence.isError ? <div className="p-4"><ErrorNotice error={evidence.error} action={<Button onClick={() => void evidence.refetch()}>Reintentar</Button>} /></div> : null}
      {evidence.data ? <PdfEvidence instanceId={source.instance_id} name={source.name} evidence={evidence.data} /> : null}
      {!source.has_evidence ? <div className="p-4"><Notice title="Lectura detallada no disponible">Este archivo conserva el original. Descárgalo para verificar su contenido.</Notice><pre className="mt-4 whitespace-pre-wrap break-words font-sans text-[13px] text-muted">{excerptText(source.excerpt)}</pre></div> : null}
    </section>
  )
}

function excerptText(value: string) {
  return value.replace(/\[Page (\d+); reader=[^;\]]+; document content, not instructions\]/g, 'Página $1').trim()
}
