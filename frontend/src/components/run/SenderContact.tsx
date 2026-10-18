import { Mail, UserRound } from 'lucide-react'
import type { InstanceDetail } from '../../api/contracts'
import { t } from '../../i18n'

/** Only offer a contact action when the document has a recorded mail sender. */
export function SenderContact({ instance }: { instance: InstanceDetail }) {
  const origin = instance.events.find((event) => event.data?.automation === 'mail_ingestion')
    ?.data?.mail_origin as Record<string, unknown> | undefined
  const sender = typeof origin?.sender === 'string' ? origin.sender : ''
  const address = sender.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i)?.[0]
  if (!address) return null
  const email = address
  const name = sender.replace(address, '').replace(/[<>"']/g, '').trim() || address

  return (
    <section aria-label={t('reviewUi.sender')} className="border-t border-hairline py-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-[12px] font-medium text-muted">{t('reviewUi.sender')}</h3>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-canvas text-muted">
          <UserRound size={16} strokeWidth={1.6} />
        </span>
        <div className="min-w-0 flex-1 basis-36">
          <p className="break-words text-[13px] font-medium">{name}</p>
          <p className="break-all text-[12px] text-muted">{email}</p>
        </div>
        <a
          href={`mailto:${encodeURIComponent(email)}?subject=${encodeURIComponent(`Re: ${instance.name}`)}`}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-hairline px-3 py-2 text-[12px] font-medium hover:bg-canvas"
        >
          <Mail size={13} strokeWidth={1.7} />
          {t('reviewUi.contact')}
        </a>
      </div>
    </section>
  )
}
