import { useRegimeCurrent, usePortfolioData } from '../../api/queries'
import { useMarketClock } from '../../hooks/useMarketClock'
import { useTheme } from '../../hooks/useTheme'
import { getRegimeColor } from '../../lib/colors'

export function Header() {
  const { data: regimeData, isLoading: regimeLoading } = useRegimeCurrent()
  const { data: portfolioData } = usePortfolioData()
  const { toggleTheme } = useTheme()
  const clockString = useMarketClock(portfolioData?.market_clock)

  return (
    <header className="sticky top-0 z-40 h-14 bg-[var(--color-surface)]/80 backdrop-blur border-b border-[var(--color-border)]">
      <div className="max-w-[1440px] mx-auto h-full px-6 flex items-center gap-2 md:gap-4">
        {/* Logo */}
        <div className="font-semibold text-lg">▲ Atlas</div>

        {/* RegimeIndicator */}
        {regimeLoading ? (
          <div className="flex items-center gap-2 text-sm">
            <span className="inline-block w-2 h-2 rounded-full bg-[var(--color-surface-alt)]" />
            <span className="text-[var(--color-text-muted)]">—</span>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-sm">
            <span
              className="inline-block rounded-full"
              style={{ width: 8, height: 8, backgroundColor: getRegimeColor(regimeData?.state) }}
            />
            <span>{regimeData?.label || regimeData?.state || '—'}</span>
            <span className="hidden md:inline-block text-[var(--color-text-muted)]">Dynamic sizing</span>
          </div>
        )}

        {/* MarketClock */}
        <span className="text-xs md:text-sm text-[var(--color-text-muted)] font-mono">{clockString}</span>

        {/* Spacer */}
        <div className="flex-1" />

        {/* AgentLink */}
        <a
          href="/homerbot"
          className="text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)] inline-flex items-center gap-1.5 min-h-[44px]"
        >
          ◈ <span className="hidden md:inline">Homerbot</span>
        </a>

        {/* ThemeToggle */}
        <button
          onClick={toggleTheme}
          className="text-lg text-[var(--color-text-muted)] hover:text-[var(--color-text)] min-w-[44px] min-h-[44px] flex items-center justify-center"
          aria-label="Toggle theme"
        >
          ◑
        </button>
      </div>
    </header>
  )
}
