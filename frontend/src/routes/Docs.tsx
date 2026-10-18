import { useEffect } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router'
import { ArrowLeft, ArrowRight } from 'lucide-react'
import { Figure } from '../components/docs/Figures'
import { Colophon, Masthead, Sheet } from '../components/landing/Chrome'
import { cn } from '../lib/cn'
import { paths } from '../lib/paths'
import { decisionBySlug, keyDecisions } from '../data/keyDecisions'

export function Docs() {
  const { slug } = useParams()
  const navigate = useNavigate()
  const current = decisionBySlug(slug)
  const index = keyDecisions.findIndex((item) => item.slug === current.slug)
  const prev = index > 0 ? keyDecisions[index - 1] : undefined
  const next = index < keyDecisions.length - 1 ? keyDecisions[index + 1] : undefined

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) {
        return
      }
      if (event.key === 'ArrowRight' && next) navigate(paths.docs(next.slug))
      if (event.key === 'ArrowLeft' && prev) navigate(paths.docs(prev.slug))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate, next, prev])

  if (slug && !keyDecisions.some((item) => item.slug === slug)) {
    return <Navigate to={paths.docs(keyDecisions[0].slug)} replace />
  }

  if (!slug) {
    return <Navigate to={paths.docs(keyDecisions[0].slug)} replace />
  }

  return (
    <div className="bg-canvas">
      <Masthead />
      <main>
        <Sheet className="grid grid-cols-1 gap-10 pb-16 pt-10 lg:grid-cols-[minmax(0,17.5rem)_minmax(0,1fr)] lg:items-start">
          <aside className="min-w-0 lg:sticky lg:top-24">
            <p className="u-kicker">Docs</p>
            <h1 className="u-display mt-3 max-w-[14ch] text-[28px]">Cinco decisiones.</h1>
            <p className="mt-3 max-w-[36ch] text-[13.5px] leading-6 text-muted">
              Contexto, alternativas, lo que elegimos, lo que perdemos y la evidencia. Lo que pide el
              tribunal.
            </p>
            <ol className="mt-6 flex gap-2 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible">
              {keyDecisions.map((item) => {
                const active = item.slug === current.slug
                return (
                  <li key={item.slug} className="shrink-0 lg:shrink">
                    <Link
                      to={paths.docs(item.slug)}
                      aria-current={active ? 'page' : undefined}
                      className={cn(
                        'flex min-w-[16rem] flex-col rounded-[14px] px-3 py-2.5 lg:min-w-0',
                        active
                          ? 'bg-white shadow-[0_1px_2px_rgba(19,19,19,0.06)] ring-1 ring-black/[0.06]'
                          : 'hover:bg-white/70',
                      )}
                    >
                      <span className="flex items-baseline justify-between gap-3">
                        <span className="text-[13.5px] font-medium tracking-[-0.02em]">
                          {item.title}
                        </span>
                        <span className="font-mono text-[11px] text-faint">{item.n}</span>
                      </span>
                      <span className="mt-0.5 text-[12px] text-muted">{item.hook}</span>
                    </Link>
                  </li>
                )
              })}
            </ol>
            <p className="mt-5 hidden font-mono text-[11px] text-faint lg:block">← → para pasar</p>
          </aside>

          <article className="min-w-0">
            <p className="font-mono text-[11px] text-faint">
              ADR {current.adrs.join(' · ')}
            </p>
            <h2 className="u-display mt-3 max-w-[22ch] text-[clamp(32px,4vw,48px)]">
              {current.claim}
            </h2>
            <p className="mt-5 max-w-[58ch] text-[16px] leading-7 text-ink">{current.say}</p>

            <div className="mt-8">
              <Figure slug={current.slug} />
            </div>

            <section className="mt-8 rounded-[18px] bg-paper px-5 py-4 ring-1 ring-black/[0.05]">
              <p className="font-mono text-[11px] text-faint">ejemplo · {current.example.file}</p>
              <p className="mt-2 max-w-[62ch] text-[14.5px] leading-7">{current.example.body}</p>
            </section>

            <dl className="mt-10 grid gap-8 border-t border-hairline pt-8 md:grid-cols-2">
              <Block title="Contexto">{current.context}</Block>
              <Block title="Lo que aceptamos perder">{current.lose}</Block>
            </dl>

            <section className="mt-8">
              <h3 className="font-mono text-[11px] tracking-[0.14em] text-faint uppercase">
                Alternativas
              </h3>
              <ul className="mt-3">
                {current.alternatives.map((item) => (
                  <li
                    key={item.name}
                    className={cn(
                      'grid gap-1 border-t border-hairline py-4 last:border-b md:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] md:gap-8',
                      !item.chosen && 'text-muted',
                    )}
                  >
                    <p className="text-[14px] tracking-[-0.02em]">
                      <span
                        className={cn(
                          'mr-2 font-mono text-[10px]',
                          item.chosen ? 'text-pagar' : 'text-faint',
                        )}
                      >
                        {item.chosen ? 'elegida' : 'no'}
                      </span>
                      {item.name}
                    </p>
                    <p className="text-[13.5px] leading-6">{item.why}</p>
                  </li>
                ))}
              </ul>
            </section>

            <Block title="Decisión" className="mt-8 max-w-[66ch]">
              {current.decision}
            </Block>

            <section className="mt-8">
              <h3 className="font-mono text-[11px] tracking-[0.14em] text-faint uppercase">
                Evidencia
              </h3>
              <ul className="mt-3">
                {current.evidence.map((item) => (
                  <li key={item} className="border-t border-hairline py-3 text-[14px] leading-6 last:border-b">
                    {item}
                  </li>
                ))}
              </ul>
            </section>

            <nav className="mt-10 flex items-center justify-between gap-4 border-t border-hairline pt-6">
              {prev ? (
                <Link
                  to={paths.docs(prev.slug)}
                  className="inline-flex items-center gap-2 text-[13px] text-muted hover:text-ink"
                >
                  <ArrowLeft size={14} strokeWidth={2} />
                  {prev.title}
                </Link>
              ) : (
                <span />
              )}
              {next ? (
                <Link
                  to={paths.docs(next.slug)}
                  className="inline-flex items-center gap-2 text-[13px] text-muted hover:text-ink"
                >
                  {next.title}
                  <ArrowRight size={14} strokeWidth={2} />
                </Link>
              ) : (
                <span />
              )}
            </nav>
          </article>
        </Sheet>
      </main>
      <Colophon />
    </div>
  )
}

function Block({
  title,
  children,
  className,
}: {
  title: string
  children: string
  className?: string
}) {
  return (
    <div className={className}>
      <h3 className="font-mono text-[11px] tracking-[0.14em] text-faint uppercase">{title}</h3>
      <p className="mt-3 text-[14.5px] leading-7">{children}</p>
    </div>
  )
}
