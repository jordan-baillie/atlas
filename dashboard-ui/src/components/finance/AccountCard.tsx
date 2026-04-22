import type { FinanceAccount } from '../../api/types'
import { fmtCcy } from '../../lib/format'

interface Props { account: FinanceAccount }

const ACCOUNT_STYLES: Record<string, { color: string; emoji: string }> = {
  saver: { color: '#22c55e', emoji: '🏦' },
  savings: { color: '#22c55e', emoji: '🏦' },
  transactional: { color: '#f59e0b', emoji: '💳' },
  spending: { color: '#f59e0b', emoji: '💳' },
  investment: { color: '#6366f1', emoji: '📈' },
}

function getStyle(type?: string) {
  if (!type) return { color: '#a1a1aa', emoji: '💰' }
  return ACCOUNT_STYLES[String(type).toLowerCase()] ?? { color: '#a1a1aa', emoji: '💰' }
}

export function AccountCard({ account }: Props) {
  const balance = account.balance ?? 0
  const limit = account.limit
  const hasLimit = limit != null && limit > 0
  const pct = hasLimit ? Math.min(100, Math.abs(balance) / (limit as number) * 100) : 0
  const { color, emoji } = getStyle(account.type)

  return (
    <div
      data-testid="account-card"
      className="bg-[var(--color-surface)] rounded-xl p-4 border border-[var(--color-border)] hover:translate-y-[-1px] hover:shadow-lg transition-all duration-200"
      style={{ borderLeftColor: color, borderLeftWidth: 3 }}
    >
      <div className="flex items-center gap-2.5 mb-3">
        <span className="text-lg">{emoji}</span>
        <div className="min-w-0 flex-1">
          <div className="font-mono font-semibold text-sm truncate">{account.name ?? '\u2014'}</div>
          {account.type != null && (
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-mono mt-0.5">
              {account.type}
            </div>
          )}
        </div>
      </div>
      <div className={`font-mono text-xl font-semibold ${balance < 0 ? 'text-[var(--color-red)]' : ''}`}>
        {fmtCcy(account.balance)}
      </div>
      {hasLimit && (
        <>
          <div className="h-2 bg-[var(--color-surface-alt)] rounded-full mt-3 overflow-hidden">
            <div
              className="h-full rounded-full"
              style={{
                width: pct + '%',
                background: `linear-gradient(90deg, ${color}, ${color}cc)`,
              }}
            />
          </div>
          <div className="flex justify-between mt-1.5 text-xs text-[var(--color-text-muted)] font-mono">
            <span>{fmtCcy(Math.abs(balance))}</span>
            <span>/ {fmtCcy(limit as number)}</span>
          </div>
        </>
      )}
    </div>
  )
}
