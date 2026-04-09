import { useFinanceData } from '../../api/queries'
import { Skeleton } from '../layout/Skeleton'
import { SectionBoundary } from '../layout/SectionBoundary'
import { FinSummaryStrip } from './FinSummaryStrip'
import { SpendingPaceChart } from './SpendingPaceChart'
import { BankAccountsGrid } from './BankAccountsGrid'
import { SpendingBars } from './SpendingBars'
import { BudgetGrid } from './BudgetGrid'
import { MonthlyComparison } from './MonthlyComparison'
import { RecurringExpenses } from './RecurringExpenses'
import { RecentTransactions } from './RecentTransactions'

export function FinanceTab() {
  const finance = useFinanceData(true)
  const data = finance.data
  if (!data) return <Skeleton className="h-96" />
  return (
    <div className="space-y-4 md:space-y-6">
      <SectionBoundary title="Summary">
        <FinSummaryStrip data={data} />
      </SectionBoundary>

      <SectionBoundary title="Spending Pace">
        {data.insights?.pace_data && data.insights.pace_data.length > 0
          ? <SpendingPaceChart
              paceData={data.insights.pace_data}
              paceStatus={data.insights.pace_status}
              paceDiff={data.insights.pace_diff}
            />
          : null}
      </SectionBoundary>

      <SectionBoundary title="Accounts">
        {data.accounts && data.accounts.length > 0
          ? <BankAccountsGrid accounts={data.accounts} />
          : null}
      </SectionBoundary>

      <SectionBoundary title="Categories">
        {data.monthly_spending?.by_parent_category && data.monthly_spending.by_parent_category.length > 0
          ? <SpendingBars
              categories={data.monthly_spending.by_parent_category}
              total={data.monthly_spending.total}
            />
          : null}
      </SectionBoundary>

      <SectionBoundary title="Budgets">
        {data.insights?.account_limits && Object.keys(data.insights.account_limits).length > 0
          ? <BudgetGrid accountLimits={data.insights.account_limits} accounts={data.accounts ?? []} />
          : null}
      </SectionBoundary>

      <SectionBoundary title="Monthly">
        {data.insights?.monthly_comparison && data.insights.monthly_comparison.length > 0
          ? <MonthlyComparison rows={data.insights.monthly_comparison} />
          : null}
      </SectionBoundary>

      <SectionBoundary title="Recurring">
        {data.insights?.recurring && data.insights.recurring.length > 0
          ? <RecurringExpenses items={data.insights.recurring} />
          : null}
      </SectionBoundary>

      <SectionBoundary title="Transactions">
        {data.recent_transactions && data.recent_transactions.length > 0
          ? <RecentTransactions transactions={data.recent_transactions} />
          : null}
      </SectionBoundary>
    </div>
  )
}
