import { preloadPortfolioTab, preloadFinanceTab, preloadResearchTab, preloadRemediationTab } from '../../App'

type TabId = 'portfolio' | 'finance' | 'research' | 'remediation'

interface TabBarProps {
  activeTab: TabId
  onChange: (tab: TabId) => void
}

const preloaders: Record<string, () => void> = {
  portfolio: preloadPortfolioTab,
  finance: preloadFinanceTab,
  research: preloadResearchTab,
  remediation: preloadRemediationTab,
}

export function TabBar({ activeTab, onChange }: TabBarProps) {
  const tabs: Array<{ id: TabId; label: string }> = [
    { id: 'portfolio', label: 'Portfolio' },
    { id: 'finance', label: 'Finance' },
    { id: 'research', label: 'Research' },
    { id: 'remediation', label: 'Remediation' },
  ]
  return (
    <div className="flex gap-1 border-b border-[var(--color-border)]">
      {tabs.map((tab) => {
        const isActive = activeTab === tab.id
        const preload = preloaders[tab.id]
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            onMouseEnter={preload}
            onFocus={preload}
            className={`py-3 px-6 min-h-[44px] inline-flex items-center font-medium text-sm transition-colors ${
              isActive
                ? 'border-b-2 border-[var(--color-accent)] text-[var(--color-text)]'
                : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]'
            }`}
          >
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}
