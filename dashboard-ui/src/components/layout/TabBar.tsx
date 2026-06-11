import { useLiveState } from '../../api/queries'
import {
  preloadCommandTab,
  preloadPortfolioTab,
  preloadForgeTab,
  preloadLiveTab,
} from '../../lib/preloaders'
import { GlyphCommand, GlyphFlame, GlyphBook, GlyphSignal } from '../ui/glyphs'
import { Beacon } from '../ui/hud'

export type TabId = 'command' | 'forge' | 'portfolio' | 'live'

interface TabBarProps {
  activeTab: TabId
  onChange: (tab: TabId) => void
}

const preloaders: Record<TabId, () => void> = {
  command: preloadCommandTab,
  forge: preloadForgeTab,
  portfolio: preloadPortfolioTab,
  live: preloadLiveTab,
}

/** Each tab carries its own section accent so the active underline tints correctly. */
const TABS: Array<{ id: TabId; label: string; section: string; Icon: typeof GlyphCommand }> = [
  { id: 'command', label: 'Command', section: 'command', Icon: GlyphCommand },
  { id: 'forge', label: 'Forge', section: 'forge', Icon: GlyphFlame },
  { id: 'portfolio', label: 'Paper Book', section: 'paper', Icon: GlyphBook },
  { id: 'live', label: 'Live', section: 'live', Icon: GlyphSignal },
]

export function TabBar({ activeTab, onChange }: TabBarProps) {
  const { data: live } = useLiveState()
  const killSwitchBlocked = live?.kill_switch?.blocked === true

  return (
    <div className="mt-1 flex gap-0.5 border-b border-[var(--color-border)] overflow-x-auto">
      {TABS.map(({ id, label, section, Icon }) => {
        const isActive = activeTab === id
        const preload = preloaders[id]
        return (
          <button
            key={id}
            data-section={section}
            onClick={() => onChange(id)}
            onMouseEnter={preload}
            onFocus={preload}
            className={[
              'py-2.5 px-3.5 min-h-[40px] inline-flex items-center gap-1.5 whitespace-nowrap',
              'text-xs tracking-[0.02em] transition-colors relative',
              isActive
                ? 'font-semibold text-[var(--color-text)] after:absolute after:bottom-0 after:left-0 after:right-0 after:h-[2px] after:bg-[var(--accent-section)] after:shadow-[0_0_8px_var(--accent-section)]'
                : 'font-medium text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
            ].join(' ')}
          >
            <Icon size={13} className={isActive ? 'text-[var(--accent-section)]' : ''} />
            {label}
            {id === 'live' && killSwitchBlocked && (
              <Beacon color="var(--mc-live)" size={4} className="ml-0.5" />
            )}
          </button>
        )
      })}
    </div>
  )
}
