import { lazy, Suspense, useState } from 'react'
import { useTheme } from './hooks/useTheme'
import { Header } from './components/layout/Header'
import { TabBar } from './components/layout/TabBar'
import { ErrorBoundary } from './components/layout/ErrorBoundary'

// Lazy-load each tab so the initial bundle is just the shell.
const PortfolioTab = lazy(() =>
  import('./components/portfolio/PortfolioTab').then((m) => ({ default: m.PortfolioTab })),
)
const ForgeTab = lazy(() =>
  import('./components/forge/ForgeTab').then((m) => ({ default: m.ForgeTab })),
)
const LiveTab = lazy(() =>
  import('./components/live/LiveTab').then((m) => ({ default: m.LiveTab })),
)

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
  const [activeTab, setActiveTab] = useState<'forge' | 'portfolio' | 'live'>('forge')

  return (
    <div className="min-h-screen bg-[var(--color-bg)] text-[var(--color-text)] overflow-x-hidden">
      <Header />
      <div className="max-w-[1440px] mx-auto px-4 md:px-6">
        <TabBar activeTab={activeTab} onChange={setActiveTab} />
        <main className="py-4 md:py-6">
          <ErrorBoundary>
            <Suspense fallback={<TabFallback />}>
              <div key={activeTab} className="animate-in">
                {activeTab === 'forge' ? <ForgeTab />
                 : activeTab === 'portfolio' ? <PortfolioTab />
                 : <LiveTab />}
              </div>
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}
