import { lazy, Suspense, useState } from 'react'
import { useTheme } from './hooks/useTheme'
import { Header } from './components/layout/Header'
import { TabBar } from './components/layout/TabBar'
import { ErrorBoundary } from './components/layout/ErrorBoundary'

// Rule: bundle-dynamic-imports — lazy-load BOTH tabs so the initial bundle
// only includes the shell (Header + TabBar + App). Each tab's recharts and
// heavy deps load on demand via its dedicated chunk.
const PortfolioTab = lazy(() =>
  import('./components/portfolio/PortfolioTab').then((m) => ({ default: m.PortfolioTab })),
)
const FinanceTab = lazy(() =>
  import('./components/finance/FinanceTab').then((m) => ({ default: m.FinanceTab })),
)

// Preload helpers for TabBar hover (bundle-conditional rule: start the network
// request before the user clicks so the chunk is already warm).
export const preloadPortfolioTab = () => import('./components/portfolio/PortfolioTab')
export const preloadFinanceTab = () => import('./components/finance/FinanceTab')
const ResearchTab = lazy(() =>
  import('./components/research/ResearchTab').then((m) => ({ default: m.ResearchTab })),
)
export const preloadResearchTab = () => import('./components/research/ResearchTab')
const RemediationTab = lazy(() =>
  import('./components/error_remediation/RemediationTab').then((m) => ({ default: m.RemediationTab })),
)
export const preloadRemediationTab = () => import('./components/error_remediation/RemediationTab')

// Skeleton matching the tab content shape — prevents layout shift during
// code-split load (async-suspense-boundaries rule).
function TabFallback() {
  return (
    <div className="space-y-4">
      <div className="h-24 bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl animate-pulse" />
      <div className="h-80 bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl animate-pulse" />
    </div>
  )
}

export default function App() {
  useTheme()
  const [activeTab, setActiveTab] = useState<'portfolio' | 'finance' | 'research' | 'remediation'>('portfolio')

  return (
    <div className="min-h-screen bg-[var(--color-bg)] text-[var(--color-text)] overflow-x-hidden">
      <Header />
      <div className="max-w-[1440px] mx-auto px-4 md:px-6">
        <TabBar activeTab={activeTab} onChange={setActiveTab} />
        <main className="py-4 md:py-6">
          <ErrorBoundary>
            <Suspense fallback={<TabFallback />}>
              <div key={activeTab} className="animate-in">
                {activeTab === 'portfolio' ? <PortfolioTab /> : activeTab === 'finance' ? <FinanceTab /> : activeTab === 'research' ? <ResearchTab /> : <RemediationTab />}
              </div>
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}
