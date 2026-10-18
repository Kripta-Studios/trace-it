import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { AgentConfigOut } from '../../api/contracts'
import { keys } from '../../api/queries'
import { t } from '../../i18n'
import { useSession } from '../../state/session'
import { Input } from '../shell/Controls'
import { Empty, ErrorNotice } from '../shell/Notice'

/** The model each agent of one use case runs. One request; a change is a new agent version. */
export function UseCaseModels({ useCaseId }: { useCaseId: number }) {
  const { isManager } = useSession()
  const queryClient = useQueryClient()
  const useCase = useQuery({
    queryKey: keys.useCase(useCaseId),
    queryFn: () => api.getUseCase(useCaseId),
  })
  const change = useMutation({
    mutationFn: ({ agent, model }: { agent: AgentConfigOut; model: string | null }) =>
      api.setAgentModel(useCaseId, agent, model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.useCase(useCaseId) }),
  })
  const agents = useCase.data?.agents ?? []

  return (
    <>
      {useCase.isError ? <ErrorNotice error={useCase.error} /> : null}
      {useCase.data && agents.length === 0 ? <Empty>{t('settings.noAgents')}</Empty> : null}
      {agents.map((agent) => (
        <div key={agent.role} className="flex items-center gap-2">
          <span className="w-32 shrink-0 font-mono text-[12px] text-muted">{agent.role}</span>
          <Input
            key={`${agent.role}:${agent.version}`}
            defaultValue={agent.config.model ?? ''}
            aria-label={`${agent.role} model`}
            readOnly={!isManager}
            disabled={change.isPending}
            className="font-mono"
            onBlur={(event) => {
              const model = event.target.value.trim() || null
              if (isManager && model !== (agent.config.model ?? null)) change.mutate({ agent, model })
            }}
          />
        </div>
      ))}
      {change.isError ? <ErrorNotice error={change.error} /> : null}
    </>
  )
}
