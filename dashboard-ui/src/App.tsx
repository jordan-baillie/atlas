import { lazy, Suspense, useState } from 'react'
import { useTheme } from './hooks/useTheme'
import { Header } from './components/layout/Header'
import { TabBar, type TabId } from './components/layout/TabBar'
import { ErrorBoundary } from './components/layout/ErrorBoundary'
import { MissionControlBackdrop } from './components/layout/MissionControlBackdrop'

// Lazy-load each tab so the initial bundle is just the shell.
const CommandTab = lazy(() =>
  import('./components/command/CommandTab').then((m) => ({ default: m.CommandTab })),
)
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
      <div className="h-24 skeleton rounded-xl" />
      <div className="h-80 skeleton rounded-xl" />
    </div>
  )
}

/** Tab id -> backdrop/accent section name (`portfolio` renders as the green Paper Book). */
const SECTION: Record<TabId, string> = {
  command: 'command',
  forge: 'forge',
  portfolio: 'paper',
  live: 'live',
}

export default function App() {
  useTheme()
  const [activeTab, setActiveTab] = useState<TabId>('command')
  const section = SECTION[activeTab]

  return (
    <div
      data-section={section}
      className="min-h-screen bg-[var(--color-bg)] text-[var(--color-text)] overflow-x-hidden"
    >
      <MissionControlBackdrop section={section} />
      <div className="relative z-10">
        <Header />
        <div className="max-w-[1440px] mx-auto px-4 md:px-6">
          <TabBar activeTab={activeTab} onChange={setActiveTab} />
          <main className="py-4 md:py-6">
            <ErrorBoundary>
              <Suspense fallback={<TabFallback />}>
                <div key={activeTab} className="animate-in">
                  {activeTab === 'command' ? <CommandTab onNavigate={setActiveTab} />
                   : activeTab === 'forge' ? <ForgeTab />
                   : activeTab === 'portfolio' ? <PortfolioTab />
                   : <LiveTab />}
                </div>
              </Suspense>
            </ErrorBoundary>
          </main>
        </div>
      </div>
    </div>
  )
}
