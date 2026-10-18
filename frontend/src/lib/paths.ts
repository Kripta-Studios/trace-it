/** The Revisión tab that lists open alerts. */
export const ALERTS_TAB = 'alertas'

export const paths = {
  landing: '/',
  docs: (slug?: string) => (slug ? `/docs/${slug}` : '/docs'),
  processes: '/processes',
  newProcess: '/processes/new',
  newProcessDraft: (draftId: number | string) => `/processes/new?draft=${draftId}`,
  /** The manager's inbox: what waits for them, and what was already decided. */
  process: (id: number | string) => `/processes/${id}`,
  history: (id: number | string) => `/processes/${id}?vista=historial`,
  /** The console: panel, definition, runs, review and settings, one tab each. */
  panel: (id: number | string) => `/processes/${id}/panel`,
  processChat: (id: number | string) => `/processes/${id}/chat`,
  /** The direct editor, kept as a fallback to the unified process chat. */
  definition: (id: number | string) => `/processes/${id}/definition`,
  definitionManual: (id: number | string) => `/processes/${id}/definition/manual`,
  definitionContext: (id: number | string) => `/processes/${id}/definition/contexto`,
  definitionInputs: (id: number | string) => `/processes/${id}/definition/inputs`,
  definitionSources: (id: number | string) => `/processes/${id}/definition/fuentes`,
  documentation: (id: number | string) => `/processes/${id}/documentation`,
  knowledge: (id: number | string) => `/processes/${id}/definition/fuentes`,
  versions: (id: number | string) => `/processes/${id}/definition/manual`,
  review: (id: number | string) => `/processes/${id}/review`,
  // reviewer-agent FE-4/5 (docs/reviewer-agent.md): a case opened in Revisión. If merging a
  // newer version from Carlos, keep his paths and preserve: review?i=<instance>.
  reviewCase: (processId: number | string, instanceId: number | string) =>
    `/processes/${processId}/review?i=${instanceId}`,
  metrics: (id: number | string) => `/processes/${id}/metrics`,
  processSettings: (id: number | string) => `/processes/${id}/settings`,
  rules: (id: number | string) => `/processes/${id}/definition/manual`,
  rule: (processId: number | string, ruleId: number | string) =>
    `/processes/${processId}/rules/${ruleId}`,
  instances: (id: number | string, runId?: number) =>
    `/processes/${id}/instances${runId ? `?run=${runId}` : ''}`,
  instance: (processId: number | string, instanceId: number | string) =>
    `/processes/${processId}/instances?i=${instanceId}`,
  queue: (id: number | string) => `/processes/${id}/review`,
  audit: (id: number | string) => `/processes/${id}/definition/manual`,
  sources: (id: number | string) => `/processes/${id}/definition/fuentes`,
  settings: '/settings',
} as const

/** The process id that owns the current screen, if any. */
export function processFromPath(pathname: string): number | undefined {
  const match = pathname.match(/\/processes\/(\d+)/)
  return match ? Number(match[1]) : undefined
}
