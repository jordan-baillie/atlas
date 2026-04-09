// Rule: rendering-content-visibility — long lists defer offscreen layout/paint
import { useState } from 'react'
import type { RecentTransaction } from '../../api/types'
import { fmtSignedCcy, fmtDateShort, pnlClass } from '../../lib/format'

interface Props { transactions: RecentTransaction[] }

export function RecentTransactions({ transactions }: Props) {
  const [expanded, setExpanded] = useState(false)
  const visible = expanded ? transactions : transactions.slice(0, 10)

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold">
          RECENT TRANSACTIONS ({transactions.length})
        </div>
        {transactions.length > 10 ? (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] font-mono"
          >
            {expanded ? 'Show less' : `Show all (${transactions.length})`}
          </button>
        ) : null}
      </div>
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl divide-y divide-[var(--color-border)]/50">
        {visible.map((tx, i) => (
          <div key={i} className="flex items-center justify-between px-4 py-3 cv-auto-sm">
            <div className="flex-1 min-w-0">
              <div className="text-sm truncate">{tx.description ?? '\u2014'}</div>
              <div className="text-xs text-[var(--color-text-muted)] font-mono mt-0.5">
                {fmtDateShort(tx.date)} · {tx.parent_category ?? tx.category ?? '\u2014'}
              </div>
            </div>
            <div className={`font-mono text-sm ml-3 ${pnlClass(tx.amount)}`}>
              {fmtSignedCcy(tx.amount)}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
