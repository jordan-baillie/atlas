import type { FinanceData } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { fmtCcy, fmtSignedCcy, fmtPct, fmtNum, pnlClass } from '../../lib/format'

interface Props { data: FinanceData }

export function FinSummaryStrip({ data }: Props) {
  return (
    <div data-testid="finance-summary-strip" className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
      <StatCard
        label="NET WORTH"
        value={`${fmtCcy(data.net_worth?.total_aud)} AUD`}
        sub={`${fmtPct(data.net_worth?.pct_invested)} invested`}
        hero
      />
      <StatCard
        label="MONTHLY SPEND"
        value={fmtCcy(data.performance?.monthly_spending_aud)}
        sub={`Budget: ${fmtCcy(data.insights?.total_monthly_budget)}`}
      />
      <StatCard
        label="SAVINGS THIS MONTH"
        value={<span className={pnlClass(data.performance?.savings_aud)}>{fmtSignedCcy(data.performance?.savings_aud)}</span>}
        sub={`Income: ${fmtCcy(data.performance?.income_aud)}`}
      />
      <StatCard
        label="RUNWAY"
        value={`${fmtNum(data.performance?.runway_months, 1)} mo`}
        sub={`FI Ratio: ${fmtPct(data.performance?.fi_ratio_pct)}`}
      />
    </div>
  )
}
