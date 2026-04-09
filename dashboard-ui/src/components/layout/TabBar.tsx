interface TabBarProps {
  activeTab: 'portfolio' | 'finance'
  onChange: (tab: 'portfolio' | 'finance') => void
}

export function TabBar({ activeTab, onChange }: TabBarProps) {
  const tabs: Array<{ id: 'portfolio' | 'finance'; label: string }> = [
    { id: 'portfolio', label: 'Portfolio' },
    { id: 'finance', label: 'Finance' },
  ]
  return (
    <div className="flex gap-1 border-b border-[var(--color-border)]">
      {tabs.map((tab) => {
        const isActive = activeTab === tab.id
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
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
