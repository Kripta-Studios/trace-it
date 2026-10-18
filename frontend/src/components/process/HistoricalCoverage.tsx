import type { ValidationReport } from '../../api/contracts'
import { Notice } from '../shell/Notice'

export function HistoricalCoverage({ validation }: { validation: Pick<ValidationReport, 'coverage' | 'not_evaluable'> }) {
  const coverage = validation.coverage
  if (!coverage) {
    return <Notice tone="warning" title="Cobertura histórica no disponible">
      Esta validación no incluye el recuento de casos históricos. Revisa el informe completo antes de publicar.
    </Notice>
  }

  const { evaluated, not_evaluable: notEvaluable, partial, none } = coverage
  const total = coverage.total ?? evaluated + notEvaluable
  const zeroCoverage = total > 0 && evaluated === 0
  const incomplete = notEvaluable > 0
  const title = total === 0
    ? 'No hay decisiones históricas que evaluar'
    : zeroCoverage
      ? 'Ninguna decisión histórica pudo evaluarse por completo'
      : incomplete
        ? 'Cobertura histórica parcial'
        : 'Todos los casos históricos pudieron evaluarse'

  return <Notice tone={total === 0 || incomplete ? 'warning' : 'neutral'} title={title}>
    <p>{evaluated} de {total} decisiones históricas evaluadas por completo; {notEvaluable} quedaron incompletas.</p>
    {notEvaluable > 0 && <>
      <p className="mt-1">{partial} conservaron resultados parciales; {none} no pudieron ejecutar ninguna regla. Faltan datos que la nueva versión exige.</p>
      {!!validation.not_evaluable?.length && <details className="mt-2">
        <summary className="cursor-pointer">Revisar casos sin cobertura completa</summary>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          {validation.not_evaluable.map(item => <li key={item.instance_id}>
            {item.name || `Caso ${item.instance_id}`}: faltan {item.missing_symbols.join(', ') || 'datos obligatorios'}
          </li>)}
        </ul>
      </details>}
    </>}
  </Notice>
}
