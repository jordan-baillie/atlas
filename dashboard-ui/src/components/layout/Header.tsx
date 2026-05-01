import { useRegimeCurrent, usePortfolioData } from '../../api/queries'
import { useMarketClock } from '../../hooks/useMarketClock'
import { useTheme } from '../../hooks/useTheme'
import { useShowAllUniverses } from '../../hooks/useShowAllUniverses'
import { getRegimeColor } from '../../lib/colors'
import { DataFreshnessChip } from './DataFreshnessChip'

export function Header() {
  const { data: regimeData, isLoading: regimeLoading } = useRegimeCurrent()
  const { data: portfolioData } = usePortfolioData()
  const { toggleTheme } = useTheme()
  const clockString = useMarketClock(portfolioData?.market_clock)
  const { showAll, setShowAll } = useShowAllUniverses()

  const regimeState = regimeData?.label || regimeData?.state || '\u2014'
  const regimeColor = getRegimeColor(regimeData?.state)

  return (
    <header className="sticky top-0 z-40 h-14 bg-[var(--color-surface)]/80 backdrop-blur-md border-b border-[var(--color-border)] shadow-sm">
      <div className="max-w-[1440px] mx-auto h-full px-6 flex items-center gap-3 md:gap-4">
        {/* Logo — mono + tight tracking for a terminal-native feel */}
        <div className="font-mono font-semibold text-base tracking-tight select-none">▲ Atlas</div>

        {/* Regime Badge */}
        {regimeLoading ? (
          <div className="h-6 w-24 rounded-full bg-[var(--color-surface-alt)] animate-pulse" />
        ) : (
          <div
            className="flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium border"
            style={{
              backgroundColor: regimeColor + '18',
              borderColor: regimeColor + '40',
              color: regimeColor,
            }}
          >
            <span
              className="w-1.5 h-1.5 rounded-full"
              style={{ backgroundColor: regimeColor }}
            />
            {regimeState}
          </div>
        )}

        {/* Dynamic Sizing label */}
        <span className="hidden md:inline text-[11px] text-[var(--color-text-muted)] font-mono">Dynamic sizing</span>

        {/* Market Clock */}
        <span className="text-xs text-[var(--color-text-muted)] font-mono tabular-nums">{clockString}</span>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Data Freshness Chip */}
        <DataFreshnessChip />

        {/* Show All Markets toggle */}
        <label className="hidden md:flex items-center gap-1.5 cursor-pointer select-none" title="Show all configured markets (including passive)">
          <span className="text-[11px] text-[var(--color-text-muted)] font-mono whitespace-nowrap">All markets</span>
          <button
            type="button"
            role="switch"
            aria-checked={showAll}
            onClick={() => setShowAll(!showAll)}
            className={`relative inline-flex h-4 w-7 items-center rounded-full border transition-colors ${
              showAll
                ? 'bg-[var(--color-accent)] border-[var(--color-accent)]'
                : 'bg-[var(--color-surface-alt)] border-[var(--color-border)]'
            }`}
          >
            <span
              className={`inline-block h-2.5 w-2.5 transform rounded-full bg-white shadow transition-transform ${
                showAll ? 'translate-x-3.5' : 'translate-x-0.5'
              }`}
            />
          </button>
        </label>

        {/* Agent Link */}
        <a
          href="/homerbot"
          className="text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors inline-flex items-center gap-1.5 min-h-[44px]"
        >
          ◈ <span className="hidden md:inline">Homerbot</span>
        </a>

        {/* Theme Toggle */}
        <button
          onClick={toggleTheme}
          className="text-lg text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors min-w-[44px] min-h-[44px] flex items-center justify-center rounded-lg hover:bg-[var(--color-surface-alt)]"
          aria-label="Toggle theme"
        >
          ◑
        </button>
      </div>
    </header>
  )
}
