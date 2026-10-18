import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { useSearchParams } from 'react-router'
import { api } from '../api/client'
import { keys } from '../api/queries'
import { Segmented, Select } from '../components/shell/Controls'
import { SettingsSection } from '../components/shell/SettingsSection'
import { UseCaseModels } from '../components/process/UseCaseModels'
import { LanguageSelect } from '../components/shell/LanguageSelect'
import { ErrorNotice } from '../components/shell/Notice'
import { Topbar } from '../components/shell/Topbar'
import { t } from '../i18n'
import { cn } from '../lib/cn'
import { useLocale } from '../state/locale'
import { useSession } from '../state/session'
import { useTheme, type Theme } from '../state/theme'

const TABS = ['general', 'identity', 'models'] as const
type Tab = (typeof TABS)[number]

export function Settings() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab') as Tab | null
  const tab: Tab = requested && TABS.includes(requested) ? requested : 'general'

  return (
    <>
      <Topbar crumbs={[{ label: t('settings.title') }]} />
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-10 pt-4 sm:px-6">
        <div className="max-w-3xl">
          <Segmented<Tab>
            value={tab}
            onChange={(next) => setParams(next === 'general' ? {} : { tab: next }, { replace: true })}
            options={TABS.map((value) => ({ value, label: t(`settings.tabs.${value}`) }))}
          />
          <div className="mt-2 divide-y divide-rule">
            {tab === 'general' ? (
              <>
                <Appearance />
                <Language />
              </>
            ) : null}
            {tab === 'identity' ? <Identity /> : null}
            {tab === 'models' ? <Models /> : null}
          </div>
        </div>
      </div>
    </>
  )
}

function Identity() {
  const { user, signIn } = useSession()
  const users = useQuery({ queryKey: keys.users, queryFn: () => api.listUsers() })
  const switchUser = useMutation({ mutationFn: (email: string) => signIn(email) })

  return (
    <SettingsSection
      title={t('settings.identity')}
      description={t('settings.identityHint')}
    >
      {users.isError ? <ErrorNotice error={users.error} /> : null}
      {switchUser.isError ? <ErrorNotice error={switchUser.error} /> : null}
      <ul className="overflow-hidden rounded-[14px] bg-surface ring-1 ring-line">
        {(users.data ?? []).map((candidate) => {
          const current = candidate.id === user?.id
          return (
            <li key={candidate.id} className="border-b border-rule last:border-b-0">
              <button
                type="button"
                aria-pressed={current}
                disabled={switchUser.isPending}
                onClick={() => switchUser.mutate(candidate.email)}
                className={cn(
                  'flex w-full items-center gap-3 px-3.5 py-2.5 text-left',
                  current ? 'bg-well/60' : 'hover:bg-canvas',
                )}
              >
                <span
                  className={cn(
                    'grid h-4 w-4 shrink-0 place-items-center rounded-full ring-1',
                    current ? 'bg-ink text-on-ink ring-ink' : 'ring-line',
                  )}
                >
                  {current ? <Check size={10} strokeWidth={2.5} /> : null}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] text-ink">{candidate.name}</span>
                  <span className="block truncate font-mono text-[11px] text-faint">
                    {candidate.email}
                  </span>
                </span>
                <span className="shrink-0 text-[12px] text-muted">
                  {t(`roles.${candidate.role}`)}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    </SettingsSection>
  )
}

function Appearance() {
  const { theme, setTheme } = useTheme()

  return (
    <SettingsSection title={t('settings.appearance')} description={t('settings.appearanceHint')}>
      <Segmented<Theme>
        value={theme}
        onChange={setTheme}
        options={[
          { value: 'light', label: t('settings.light') },
          { value: 'dark', label: t('settings.dark') },
        ]}
      />
    </SettingsSection>
  )
}

function Language() {
  const { locale, setLocale } = useLocale()

  return (
    <SettingsSection title={t('settings.language')} description={t('settings.languageHint')}>
      <LanguageSelect value={locale} onChange={setLocale} />
    </SettingsSection>
  )
}

function Models() {
  const useCases = useQuery({ queryKey: keys.useCases, queryFn: () => api.listUseCases() })
  const [picked, setPicked] = useState<number | null>(null)
  const useCaseId = picked ?? useCases.data?.[0]?.id

  return (
    <SettingsSection
      title={t('settings.models')}
      description={t('settings.modelsHint')}
    >
      <div className="space-y-2">
        {useCases.isError ? <ErrorNotice error={useCases.error} /> : null}
        {(useCases.data?.length ?? 0) > 1 ? (
          <Select value={useCaseId} onChange={(event) => setPicked(Number(event.target.value))}>
            {useCases.data?.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </Select>
        ) : null}
        {useCaseId != null ? <UseCaseModels useCaseId={useCaseId} /> : null}
      </div>
    </SettingsSection>
  )
}
