import type { FinanceAccount } from '../../api/types'
import { BudgetCard } from './BudgetCard'

interface Props {
  accountLimits: Record<string, number>
  accounts: FinanceAccount[]
}

export function BudgetGrid({ accountLimits, accounts }: Props) {
  const entries = Object.entries(accountLimits)
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">
        BUDGETS ({entries.length})
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {entries.map(([name, limit]) => {
          const acct = accounts.find(a => a.name === name)
          const spent = Math.abs(acct?.balance ?? 0)
          return <BudgetCard key={name} name={name} limit={limit} spent={spent} />
        })}
      </div>
    </div>
  )
}
