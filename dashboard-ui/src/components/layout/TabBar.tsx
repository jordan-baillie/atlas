import { preloadPortfolioTab, preloadForgeTab, preloadLiveTab } from '../../lib/preloaders'

type TabId = 'forge' | 'portfolio' | 'live'

interface TabBarProps {
  activeTab: TabId
  onChange: (tab: TabId) => void
}

const preloaders: Record<string, () => void> = {
  forge: preloadForgeTab,
  portfolio: preloadPortfolioTab,
  live: preloadLiveTab,
}

export function TabBar({ activeTab, onChange }: TabBarProps) {
  const tabs: Array<{ id: TabId; label: string }> = [
    { id: 'forge', label: '🔥 Forge' },
    { id: 'portfolio', label: 'Paper Book' },
    { id: 'live', label: 'Live' },
  ]

  return (
    <div className="mt-1 flex gap-0.5 border-b border-[var(--color-border)]">
      {tabs.map((tab) => {
        const isActive = activeTab === tab.id
        const preload = preloaders[tab.id]
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            onMouseEnter={preload}
            onFocus={preload}
            className={[
              'py-2.5 px-3.5 min-h-[40px] inline-flex items-center',
              'text-xs tracking-[0.02em] transition-colors relative',
              isActive
                ? 'font-semibold text-[var(--color-text)] after:absolute after:bottom-0 after:left-0 after:right-0 after:h-[2px] after:bg-[var(--color-accent)] after:rounded-none'
                : 'font-medium text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
            ].join(' ')}
          >
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}
