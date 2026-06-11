import { usePortfolioData, useLiveState } from '../../api/queries'
import { useForgeState } from '../../api/forge-queries'
import { useMarketClock } from '../../hooks/useMarketClock'
import { HudPanel, Beacon } from '../ui/hud'
import { GlyphShield, GlyphClock, GlyphFlame } from '../ui/glyphs'

/** Command hero: kill-switch beacon (dominant), market clock, forge status. */
export function HeroStrip() {
  const { data: live } = useLiveState()
  const { data: portfolio } = usePortfolioData()
  const { data: forge } = useForgeState()
  const clockString = useMarketClock(portfolio?.market_clock)
  const marketOpen = portfolio?.market_clock?.is_open === true

  const blocked = live?.kill_switch?.blocked === true
  const ksColor = blocked ? 'var(--mc-live)' : 'var(--color-positive)'
  // "HOT" = a cycle is executing right now; "ARMED" = enabled, awaiting nightly run
  const forgeHot = forge?.status?.cycle_active === true
  const forgeArmed = forge?.status?.running === true

  return (
    <HudPanel brackets glow glowPulse={blocked} className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        {/* Kill switch — the dominant element */}
        <div className="flex items-center gap-3 min-w-0">
          <Beacon color={ksColor} on size={9} />
          <div className="leading-tight min-w-0">
            <div className="display-num text-xl sm:text-2xl" style={{ color: ksColor }}>
              {blocked ? 'HALTED' : 'SYSTEMS NOMINAL'}
            </div>
            <div className="text-[10px] uppercase tracking-[0.22em] text-[var(--color-text-muted)] flex items-center gap-1 truncate">
              <GlyphShield size={10} />
              {blocked
                ? `kill-switch ${live?.kill_switch?.layer ?? ''} — ${live?.kill_switch?.reason ?? 'tripped'}`
                : 'kill-switch clear'}
            </div>
          </div>
        </div>

        <div className="hidden md:block w-px h-9 bg-[var(--color-border)]" aria-hidden />

        {/* Market clock */}
        <div className="leading-tight">
          <div className="display-num text-base sm:text-lg flex items-center gap-2">
            <Beacon color={marketOpen ? 'var(--color-positive)' : 'var(--color-muted)'} on={marketOpen} size={5} />
            {clockString || '—'}
          </div>
          <div className="text-[10px] uppercase tracking-[0.22em] text-[var(--color-text-muted)] flex items-center gap-1">
            <GlyphClock size={10} /> NYSE
          </div>
        </div>

        <div className="hidden md:block w-px h-9 bg-[var(--color-border)]" aria-hidden />

        {/* Forge status */}
        <div className="leading-tight" data-section="forge">
          <div
            className="display-num text-base sm:text-lg flex items-center gap-2"
            style={{ color: forgeHot ? 'var(--mc-forge-hot)' : undefined }}
          >
            <Beacon color="var(--mc-forge)" on={forgeHot} size={5} />
            {forgeHot ? 'FORGE HOT' : forgeArmed ? 'FORGE ARMED' : 'FORGE IDLE'}
          </div>
          <div className="text-[10px] uppercase tracking-[0.22em] text-[var(--color-text-muted)] flex items-center gap-1">
            <GlyphFlame size={10} />
            {forgeHot ? 'cycle in progress' : forge?.status?.next_run_str ? `next ${forge.status.next_run_str}` : 'nightly schedule'}
          </div>
        </div>
      </div>
    </HudPanel>
  )
}
