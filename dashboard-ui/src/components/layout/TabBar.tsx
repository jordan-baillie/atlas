import { preloadPortfolioTab, preloadFinanceTab, preloadResearchTab, preloadRemediationTab, preloadControlsTab } from '../../App'
import { FEATURE_CONTROLS_TAB } from '../../lib/featureFlags'

type TabId = 'portfolio' | 'finance' | 'research' | 'remediation' | 'controls'

interface TabBarProps {
  activeTab: TabId
  onChange: (tab: TabId) => void
}

const preloaders: Record<string, () => void> = {
  portfolio: preloadPortfolioTab,
  finance: preloadFinanceTab,
  research: preloadResearchTab,
  remediation: preloadRemediationTab,
  controls: preloadControlsTab,
}

export function TabBar({ activeTab, onChange }: TabBarProps) {
  const tabs: Array<{ id: TabId; label: string }> = [
    { id: 'portfolio', label: 'Portfolio' },
    { id: 'finance', label: 'Finance' },
    { id: 'research', label: 'Research' },
    { id: 'remediation', label: 'Remediation' },
    ...(FEATURE_CONTROLS_TAB ? [{ id: 'controls' as const, label: 'Controls' }] : []),
  ]
  return (
    <div className="flex gap-0.5 border-b border-[var(--color-border)]">
      {tabs.map((tab) => {
        const isActive = activeTab === tab.id
        const preload = preloaders[tab.id]
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            onMouseEnter={preload}
            onFocus={preload}
            className={`py-2 px-4 min-h-[40px] inline-flex items-center font-medium text-xs tracking-wide transition-colors relative ${
              isActive
                ? 'text-[var(--color-text)] after:absolute after:bottom-0 after:left-0 after:right-0 after:h-0.5 after:bg-[var(--color-accent)] after:rounded-t'
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
