import { Download, Minus, Plus, RotateCw, Search } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { ExtractionResult } from '../../api/contracts'
import { keys } from '../../api/queries'
import { cn } from '../../lib/cn'
import { highlight } from '../../lib/highlight'
import { symbolLabel } from '../../lib/symbols'
import { ErrorNotice } from '../shell/Notice'
import { PdfEvidence } from './PdfEvidence'

/**
 * A stored invoice PDF opens on its original pages with each reading boxed (PdfEvidence).
 * Otherwise, the stored file as the backend serves it. Searching switches to the text the
 * extraction read, with each field next to the symbol it feeds.
 */
export function DocumentPane({
  instanceId,
  name,
  embedded = false,
  initialSymbol,
}: {
  instanceId: number | undefined
  name: string | undefined
  embedded?: boolean
  initialSymbol?: string
}) {
  const [query, setQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [zoom, setZoom] = useState(1)
  const [rotation, setRotation] = useState(0)
  const evidence = useQuery({
    queryKey: keys.document(instanceId ?? 0),
    queryFn: () => api.getDocument(instanceId!),
    enabled: Boolean(instanceId),
  })

  const bumpZoom = (delta: number) => {
    setZoom((value) => Math.min(2, Math.max(0.5, Math.round((value + delta) * 10) / 10)))
  }

  if (instanceId && evidence.isPending) {
    return <p role="status" className="p-5 text-[13px] text-muted">Loading document evidence…</p>
  }
  if (instanceId && evidence.isError) {
    return (
      <div role="alert" className="p-5 text-[13px] text-muted">
        <p>Could not load document evidence. {evidence.error.message}</p>
        <button className="mt-2 underline" onClick={() => evidence.refetch()}>Retry</button>
      </div>
    )
  }
  if (instanceId && evidence.data?.kind === 'invoice') {
    return <PdfEvidence key={`${instanceId}:${evidence.data.id}`} instanceId={instanceId} name={name ?? evidence.data.file_id} evidence={evidence.data} initialSymbol={initialSymbol} />
  }

  return (
    <section
      className={cn(
        'flex h-full min-h-0 min-w-0 flex-1 flex-col',
        embedded ? '' : 'px-2 pb-3',
      )}
    >
      <div
        className={cn(
          'flex gap-3',
          embedded ? 'items-center px-3 py-2' : 'items-end justify-between px-3 pb-3 pt-1',
        )}
      >
        {embedded ? null : (
          <div>
            <p className="text-[11px] text-muted">Documento</p>
            <h2 className="text-[20px] font-medium tracking-[-0.03em]">
              {name ?? 'Sin archivo'}
            </h2>
          </div>
        )}
        <div
          className={cn(
            'flex items-center gap-1 rounded-full bg-canvas px-1 py-1 ring-1 ring-line',
            embedded && 'ml-auto',
          )}
        >
          <IconBtn
            label="Buscar en el documento"
            pressed={searchOpen}
            onClick={() => setSearchOpen((open) => !open)}
          >
            <Search size={15} strokeWidth={1.5} />
          </IconBtn>
          <IconBtn
            label="Rotar 90°"
            onClick={() => setRotation((value) => (value + 90) % 360)}
          >
            <RotateCw size={15} strokeWidth={1.5} />
          </IconBtn>
          <IconBtn label="Alejar" onClick={() => bumpZoom(-0.1)}>
            <Minus size={15} strokeWidth={1.5} />
          </IconBtn>
          <button
            type="button"
            onClick={() => {
              setZoom(1)
              setRotation(0)
            }}
            className="min-w-[3rem] px-1 font-mono text-[11px] text-ink"
            title="Restablecer"
          >
            {Math.round(zoom * 100)}%
          </button>
          <IconBtn label="Acercar" onClick={() => bumpZoom(0.1)}>
            <Plus size={15} strokeWidth={1.5} />
          </IconBtn>
          <IconBtn
            label="Descargar original"
            onClick={() => name && instanceId && downloadFile(api.fileUrl(instanceId), name)}
          >
            <Download size={15} strokeWidth={1.5} />
          </IconBtn>
        </div>
      </div>

      {searchOpen ? (
        <div className="px-3 pb-2">
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="NIF, IBAN, pedido…"
            className="w-full rounded-full bg-canvas px-3 py-1.5 text-[13px] outline-none ring-1 ring-line placeholder:text-faint"
          />
        </div>
      ) : null}

      <div
        className={cn(
          'relative min-h-0 flex-1 overflow-auto bg-canvas',
          embedded ? '' : 'rounded-[16px] ring-1 ring-line',
        )}
        onWheel={(event) => {
          if (!event.ctrlKey && !event.metaKey) return
          event.preventDefault()
          bumpZoom(event.deltaY > 0 ? -0.1 : 0.1)
        }}
      >
        <div className="flex min-h-full justify-center p-8">
          {!name ? (
            <p className="self-center text-[13px] text-muted">Selecciona un archivo de la cola.</p>
          ) : evidence.isError ? (
            <div className="max-w-[420px] self-center">
              <ErrorNotice error={evidence.error} />
            </div>
          ) : (
            <div
              style={{
                width: 560,
                transform: `rotate(${rotation}deg) scale(${zoom})`,
                transformOrigin: 'top center',
              }}
            >
              {searchOpen && evidence.data ? (
                <EvidencePaper evidence={evidence.data} query={query} />
              ) : instanceId ? (
                <iframe
                  src={api.fileUrl(instanceId)}
                  title={name}
                  style={{ width: '100%', aspectRatio: '1 / 1.414', border: 0 }}
                />
              ) : null}
            </div>
          )}
        </div>
      </div>

      {embedded ? null : evidence.data ? (
        <div className="flex items-center justify-between px-3 pt-2 text-[12px] text-muted">
          <span className="font-mono">{evidence.data.sha256.slice(0, 12)}</span>
          <span>
            {evidence.data.pipeline_version} · {evidence.data.cache_hit ? 'caché' : 'extraído'}
          </span>
        </div>
      ) : null}
    </section>
  )
}

function EvidencePaper({
  evidence,
  query,
}: {
  evidence: ExtractionResult
  query: string
}) {
  return (
    <article className="rounded-[12px] bg-paper px-9 py-8 text-ink shadow-float ring-1 ring-line">
      <div className="flex items-start justify-between gap-5 border-b border-hairline pb-5">
        <div>
          <p className="text-[11px] uppercase tracking-[0.08em] text-muted">Documento leído</p>
          <h1 className="mt-1 font-mono text-[16px]">{evidence.file_id}</h1>
        </div>
        <span className="rounded-full bg-ocr-soft px-2 py-0.5 font-mono text-[10px] text-ocr">
          {evidence.kind}
        </span>
      </div>

      <dl className="mt-5 grid grid-cols-2 gap-x-6">
        {Object.entries(evidence.fields ?? {}).map(([name, field]) => (
          <div key={name} className="border-b border-hairline py-2">
            <dt className="font-mono text-[10.5px] text-faint">
              {symbolLabel(field.symbol ?? name)}
            </dt>
            <dd className="mt-0.5 break-words font-mono text-[12.5px]">
              {highlight(field.value ?? '—', query)}
            </dd>
            {field.selected_by ? (
              <p className="mt-0.5 text-[10px] text-faint">
                {field.selected_by}
                {field.confidence != null ? ` · ${Math.round(field.confidence * 100)} %` : ''}
              </p>
            ) : null}
          </div>
        ))}
      </dl>

      {evidence.text ? (
        <pre className="mt-6 max-h-[320px] overflow-auto whitespace-pre-wrap border-t border-hairline pt-5 font-mono text-[11px] leading-5 text-muted">
          {highlight(evidence.text, query)}
        </pre>
      ) : null}
    </article>
  )
}

function IconBtn({
  label,
  children,
  onClick,
  pressed,
}: {
  label: string
  children: ReactNode
  onClick?: () => void
  pressed?: boolean
}) {
  return (
    <button
      type="button"
      title={label}
      aria-pressed={pressed}
      onClick={onClick}
      className={`grid h-7 w-7 place-items-center rounded-full hover:bg-surface hover:text-ink ${
        pressed ? 'bg-surface text-ink shadow-lift' : ''
      }`}
    >
      {children}
    </button>
  )
}

function downloadFile(url: string, name: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
}
