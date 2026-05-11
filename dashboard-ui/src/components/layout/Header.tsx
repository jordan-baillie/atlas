import { useRegimeCurrent, usePortfolioData } from '../../api/queries'
import { useMarketClock } from '../../hooks/useMarketClock'
import { useTheme } from '../../hooks/useTheme'
import { useShowAllUniverses } from '../../hooks/useShowAllUniverses'
import { DataFreshnessChip } from './DataFreshnessChip'
import { Badge } from '../shared/Badge'
import { StatusDot } from '../shared/StatusDot'
import type { BadgeVariant } from '../shared/Badge'

// ── Helpers ───────────────────────────────────────────────────────────────

/** Maps a regime state string to the nearest Badge semantic variant */
function regimeToBadgeVariant(state: string | null | undefined): BadgeVariant {
  if (!state) return 'neutral'
  if (state.startsWith('bull')) return 'success'
  if (state.startsWith('bear')) return 'danger'
  if (state.startsWith('recovery')) return 'info'
  if (state.startsWith('transition')) return 'warning'
  return 'neutral'
}

/** Maps a badge variant to a StatusDot status */
function variantToStatus(v: BadgeVariant): 'green' | 'amber' | 'red' | 'gray' {
  if (v === 'success' || v === 'info') return 'green'
  if (v === 'warning') return 'amber'
  if (v === 'danger') return 'red'
  return 'gray'
}

// ── Component ─────────────────────────────────────────────────────────────

export function Header() {
  const { data: regimeData, isLoading: regimeLoading } = useRegimeCurrent()
  const { data: portfolioData } = usePortfolioData()
  const { toggleTheme } = useTheme()
  const clockString = useMarketClock(portfolioData?.market_clock)
  const { showAll, setShowAll } = useShowAllUniverses()

  const regimeState = regimeData?.label || regimeData?.state || '\u2014'
  const badgeVariant = regimeToBadgeVariant(regimeData?.state)
  const isOpen = portfolioData?.market_clock?.is_open === true

  return (
    <header className="sticky top-0 z-40 h-14 bg-[var(--color-surface)]/80 backdrop-blur-md border-b border-[var(--color-border)] shadow-sm">
      <div className="max-w-[1440px] mx-auto h-full px-6 flex items-center gap-3 md:gap-4">

        {/* Logo — mono, tight tracking, faint accent dot */}
        <div className="font-mono font-semibold text-base tracking-[-0.03em] select-none flex items-center gap-1">
          ▲ Atlas
          <span
            className="w-1 h-1 rounded-full bg-[var(--color-accent)] opacity-60 ml-0.5"
            aria-hidden="true"
          />
        </div>

        {/* Regime Badge — variant from color family, StatusDot for live feel */}
        {regimeLoading ? (
          <div className="h-5 w-24 rounded-full skeleton" />
        ) : (
          <Badge
            variant={badgeVariant}
            size="sm"
            icon={
              <StatusDot
                status={variantToStatus(badgeVariant)}
                size="sm"
                pulse
              />
            }
          >
            {regimeState}
          </Badge>
        )}

        {/* Dynamic Sizing — subtle bordered pill */}
        <span className="hidden md:inline text-[11px] text-[var(--color-text-muted)] font-mono border border-[var(--color-border)] rounded-full px-2.5 py-0.5 leading-none">
          Dynamic sizing
        </span>

        {/* Market Clock — full text color + live market indicator */}
        <span className="text-xs text-[var(--color-text)] font-mono tabular-nums flex items-center gap-1.5">
          <StatusDot
            status={isOpen ? 'green' : 'gray'}
            size="sm"
            pulse={isOpen}
          />
          {clockString}
        </span>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Data Freshness Chip — leave alone */}
        <DataFreshnessChip />

        {/* Show All Markets toggle */}
        <label
          className="hidden md:flex items-center gap-1.5 cursor-pointer select-none"
          title="Show all configured markets (including passive)"
        >
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-mono whitespace-nowrap">
            All markets
          </span>
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

        {/* Agent Link — 36×36 hit area */}
        <a
          href="/homerbot"
          className="text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)]
                     transition-colors inline-flex items-center justify-center
                     w-9 h-9 rounded-lg hover:bg-[var(--color-surface-alt)]
                     focus:outline-none focus:ring-2 focus:ring-[var(--color-border)]"
          aria-label="Homerbot"
          title="Homerbot"
        >
          ◈
        </a>

        {/* Theme Toggle — 36×36 hit area */}
        <button
          onClick={toggleTheme}
          className="text-lg text-[var(--color-text-muted)] hover:text-[var(--color-text)]
                     transition-colors w-9 h-9 flex items-center justify-center
                     rounded-lg hover:bg-[var(--color-surface-alt)]
                     focus:outline-none focus:ring-2 focus:ring-[var(--color-border)]"
          aria-label="Toggle theme"
        >
          ◑
        </button>
      </div>
    </header>
  )
}
