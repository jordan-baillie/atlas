import { usePortfolioData } from '../../api/queries'
import { useMarketClock } from '../../hooks/useMarketClock'
import { useTheme } from '../../hooks/useTheme'
import { DataFreshnessChip } from './DataFreshnessChip'
import { Beacon } from '../ui/hud'

export function Header() {
  const { data: portfolioData } = usePortfolioData()
  const { toggleTheme } = useTheme()
  const clockString = useMarketClock(portfolioData?.market_clock)
  const isOpen = portfolioData?.market_clock?.is_open === true

  return (
    <header className="sticky top-0 z-40 h-14 bg-[var(--color-surface)]/75 backdrop-blur-md border-b border-[var(--color-border)]">
      {/* section-tinted hairline */}
      <div
        aria-hidden
        className="absolute bottom-[-1px] left-0 right-0 h-[2px]"
        style={{
          background:
            'linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent-section, var(--color-accent)) 55%, transparent), transparent)',
          transition: 'background 400ms ease',
        }}
      />
      <div className="max-w-[1440px] mx-auto h-full px-6 flex items-center gap-3 md:gap-4">

        {/* Logo + mission caption */}
        <div className="select-none leading-none">
          <div className="font-mono font-semibold text-base tracking-[-0.03em] flex items-center gap-1">
            ▲ Atlas
            <span className="w-1 h-1 rounded-full bg-[var(--accent-section,var(--color-accent))] opacity-70 ml-0.5" aria-hidden="true" />
          </div>
          <div className="hidden sm:block text-[8.5px] tracking-[0.32em] uppercase text-[var(--color-text-muted)] mt-0.5">
            Mission Control
          </div>
        </div>

        {/* Market Clock */}
        <span className="text-xs display-num flex items-center gap-1.5">
          <Beacon color={isOpen ? 'var(--color-positive)' : 'var(--color-muted)'} on={isOpen} size={4} />
          {clockString}
        </span>

        <div className="flex-1" />

        <DataFreshnessChip />

        <a
          href="/homerbot"
          className="text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors inline-flex items-center justify-center w-9 h-9 rounded-lg hover:bg-[var(--color-surface-alt)] focus:outline-none focus:ring-2 focus:ring-[var(--color-border)]"
          aria-label="Homerbot"
          title="Homerbot"
        >
          ◈
        </a>

        <button
          onClick={toggleTheme}
          className="text-lg text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors w-9 h-9 flex items-center justify-center rounded-lg hover:bg-[var(--color-surface-alt)] focus:outline-none focus:ring-2 focus:ring-[var(--color-border)]"
          aria-label="Toggle theme"
        >
          ◑
        </button>
      </div>
    </header>
  )
}
