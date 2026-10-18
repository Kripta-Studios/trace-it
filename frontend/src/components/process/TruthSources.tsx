import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { keys } from '../../api/queries'
import { DropZone } from './DropZone'
import { Button, Field, Input } from '../shell/Controls'
import { DataTable } from '../shell/DataTable'
import { Empty, ErrorNotice, Notice } from '../shell/Notice'
import { StatusBadge } from '../shell/StatusBadge'
import { NestedCard } from '../shell/Well'
import { t } from '../../i18n'
import { formatRunDate } from '../../lib/format'

/** Maestros and ERP. Not the invoice lote. */
export function TruthSources({ processId }: { processId: number }) {
  const queryClient = useQueryClient()
  const [typedCutOff, setTypedCutOff] = useState<string | null>(null)
  const sources = useQuery({
    queryKey: keys.sources(processId),
    queryFn: () => api.listSources(processId),
  })
  // Only once a workbook loaded `parameters` is there a current cut-off date to read.
  const loaded = sources.data?.some((source) => source.name === 'parameters') ?? false
  const parameters = useQuery({
    queryKey: keys.source(processId, 'parameters'),
    queryFn: () => api.getSource(processId, 'parameters'),
    enabled: loaded,
  })
  const currentCutOff = parameters.data?.data[0]?.cut_off_date
  const cutOffDate = typedCutOff ?? (typeof currentCutOff === 'string' ? currentCutOff : '')
  const erp = sources.data?.find((source) => source.name === 'erp')

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: keys.sources(processId) })
    void queryClient.invalidateQueries({ queryKey: keys.summary(processId) })
  }

  const workbook = useMutation({
    mutationFn: (file: File) => api.uploadWorkbook(processId, file, cutOffDate),
    onSuccess: () => {
      setTypedCutOff(null)
      refresh()
    },
  })
  const sync = useMutation({
    mutationFn: () => api.syncSource(processId, 'erp'),
    // A failed sync records the source as down, so the table changes either way.
    onSettled: refresh,
  })
  const diff = sync.data?.diff

  if (sources.isPending) return <Notice title="Cargando fuentes…" />
  if (sources.isError) {
    return (
      <div className="space-y-3">
        <ErrorNotice error={sources.error} />
        <Button onClick={() => void sources.refetch()} disabled={sources.isFetching}>Reintentar</Button>
      </div>
    )
  }

  return (
    <div className="space-y-8">
      <div className="grid gap-3">
        <NestedCard label="excel de referencia">
          <div className="space-y-2 px-3.5 py-3">
            {parameters.isError ? (
              <div className="space-y-2">
                <ErrorNotice error={parameters.error} />
                <Button onClick={() => void parameters.refetch()} disabled={parameters.isFetching}>Reintentar parámetros</Button>
              </div>
            ) : null}
            <Field label="Fecha de corte" hint="Los cruces con fechas se evalúan contra este día.">
              <Input
                type="date"
                required
                value={cutOffDate}
                onChange={(event) => setTypedCutOff(event.target.value)}
              />
            </Field>
            <DropZone
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              disabled={workbook.isPending || !cutOffDate}
              label={workbook.isPending ? 'Cargando el Excel…' : 'Proveedores, pedidos, parámetros'}
              hint="Un workbook. Cada hoja entra como una fuente."
              onFiles={(incoming) => {
                const file = incoming[0]
                if (file) workbook.mutate(file)
              }}
            />
            {workbook.isError ? <ErrorNotice error={workbook.error} /> : null}
            {workbook.isSuccess && workbook.data.warnings.length > 0 ? (
              <Notice tone="warning" title={`${workbook.data.warnings.length} avisos al cargar el Excel`}>
                Revisa los datos incompletos y las hojas que no se han incorporado.
                <details className="mt-2">
                  <summary className="cursor-pointer">Ver detalles</summary>
                  <ul className="mt-2 max-h-60 space-y-1 overflow-y-auto">
                    {workbook.data.warnings.map((warning, index) => (
                      <li key={index}>{workbookWarning(warning)}</li>
                    ))}
                  </ul>
                </details>
              </Notice>
            ) : null}
          </div>
        </NestedCard>

        <NestedCard label="erp">
          <div className="space-y-2 px-3.5 py-3">
            <div className="flex items-center justify-between gap-3 rounded-[12px] bg-well px-3 py-3 ring-1 ring-line">
              <div className="min-w-0">
                <p className="text-[13px] text-ink">Conector del ERP</p>
                <p className="truncate text-[11px] text-muted">
                  {erp?.origin ?? t('sourceStatus.none')} · cada descarga se guarda como una carga más
                </p>
              </div>
              <Button onClick={() => sync.mutate()} disabled={sync.isPending} className="shrink-0">
                <RefreshCw size={12} strokeWidth={2} />
                {sync.isPending ? 'Descargando…' : 'Sincronizar'}
              </Button>
            </div>
            {sync.isError ? <ErrorNotice error={sync.error} /> : null}
            {diff ? (
              <Notice
                title={`+${diff.added.length} −${diff.removed.length} ~${Object.keys(diff.changed).length} filas`}
              />
            ) : null}
          </div>
        </NestedCard>
      </div>

      <section>
        <p className="mb-3 font-mono text-[11px] tracking-[0.12em] text-faint">CARGAS</p>
        <NestedCard label={`${sources.data?.length ?? 0} fuentes`}>
          {sources.data?.length ? (
            <DataTable
              framed={false}
              rows={sources.data.map((source) => ({ ...source, id: source.name }))}
              columns={[
                {
                  key: 'name',
                  header: 'Nombre',
                  width: '10rem',
                  render: (row) => <span className="font-mono text-[12px]">{row.name}</span>,
                },
                {
                  key: 'origin',
                  header: 'Origen',
                  render: (row) => <span className="text-[12px] text-muted">{row.origin}</span>,
                },
                {
                  key: 'rows',
                  header: 'Filas',
                  width: '6rem',
                  render: (row) => <span className="font-mono text-[12px]">{row.rows}</span>,
                },
                {
                  key: 'status',
                  header: 'Estado',
                  width: '9rem',
                  render: (row) => (
                    <span title={row.error ?? undefined}>
                      <StatusBadge value={row.status ?? 'none'}>
                        {t(`sourceStatus.${row.status ?? 'none'}`)}
                      </StatusBadge>
                    </span>
                  ),
                },
                {
                  key: 'loaded_at',
                  header: 'Cargada',
                  width: '10rem',
                  render: (row) => (
                    <span className="text-[12px] text-muted">{formatRunDate(row.loaded_at)}</span>
                  ),
                },
              ]}
            />
          ) : (
            <Empty>Sin fuentes. Sin ellas, ninguna regla que cruce datos puede decidir.</Empty>
          )}
        </NestedCard>
      </section>
    </div>
  )
}

function workbookWarning(warning: Record<string, unknown>): string {
  const labels: Record<string, string> = {
    DUPLICATE_IDENTICAL: 'Fila duplicada idéntica',
    FIELD_MISSING: 'Falta un campo',
    UNRECOGNIZED_SCHEMA: 'Hoja no reconocida',
    FORMULA_UNEVALUATED: 'Fórmula sin evaluar',
  }
  const code = typeof warning.code === 'string' ? warning.code : 'Aviso'
  const details = [warning.locator, warning.sheet, warning.field, warning.identity]
    .filter((value): value is string => typeof value === 'string' && value.length > 0)
  return [labels[code] ?? code, ...details].join(' · ')
}
