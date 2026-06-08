import { lazy, Suspense, useState } from 'react'
import { useTheme } from './hooks/useTheme'
import { Header } from './components/layout/Header'
import { TabBar } from './components/layout/TabBar'
import { ErrorBoundary } from './components/layout/ErrorBoundary'

// Rule: bundle-dynamic-imports — lazy-load BOTH tabs so the initial bundle
// only includes the shell (Header + TabBar + App). Each tab's chart.js and
// heavy deps load on demand via its dedicated chunk.
const PortfolioTab = lazy(() =>
  import('./components/portfolio/PortfolioTab').then((m) => ({ default: m.PortfolioTab })),
)
const FinanceTab = lazy(() =>
  import('./components/finance/FinanceTab').then((m) => ({ default: m.FinanceTab })),
)

// Preload helpers now live in their sole consumer's import path
// (src/lib/preloaders.ts).  TabBar imports them directly from there.
// Re-exporting them from App.tsx would mix non-component exports with the
// default component export and break Fast Refresh
// (react-refresh/only-export-components).

const ForgeTab = lazy(() =>
  import('./components/forge/ForgeTab').then((m) => ({ default: m.ForgeTab })),
)
const ControlsTab = lazy(() =>
  import('./components/controls/ControlsTab').then((m) => ({ default: m.ControlsTab })),
)
const MidasTab = lazy(() =>
  import('./components/midas/MidasTab').then((m) => ({ default: m.MidasTab })),
)

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
  const [activeTab, setActiveTab] = useState<'portfolio' | 'finance' | 'forge' | 'controls' | 'midas'>('portfolio')

  return (
    <div className="min-h-screen bg-[var(--color-bg)] text-[var(--color-text)] overflow-x-hidden">
      <Header />
      <div className="max-w-[1440px] mx-auto px-4 md:px-6">
        <TabBar activeTab={activeTab} onChange={setActiveTab} />
        <main className="py-4 md:py-6">
          <ErrorBoundary>
            <Suspense fallback={<TabFallback />}>
              <div key={activeTab} className="animate-in">
                {activeTab === 'portfolio' ? <PortfolioTab />
                 : activeTab === 'finance' ? <FinanceTab />
                 : activeTab === 'forge' ? <ForgeTab />
                 : activeTab === 'midas' ? <MidasTab />
                 : <ControlsTab />}
              </div>
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}
