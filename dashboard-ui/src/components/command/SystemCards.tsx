import type { ReactNode } from 'react'
import { usePortfolioData, useLiveState } from '../../api/queries'
import { useForgeState } from '../../api/forge-queries'
import { useCelebration } from '../../hooks/useCelebration'
import { fmtCcy, fmtSignedCcy } from '../../lib/format'
import { AnimatedNumber } from '../ui/AnimatedNumber'
import { CornerBrackets, Beacon, GateStatusPill } from '../ui/hud'
import { GlyphFlame, GlyphBook, GlyphSignal } from '../ui/glyphs'
import { Chart } from '../shared/Chart'
import type { TabId } from '../layout/TabBar'

// ── shared shell ─────────────────────────────────────────────────────────────

interface ShellProps {
  section: string
  title: string
  icon: ReactNode
  beaconOn?: boolean
  celebrate?: boolean
  onClick: () => void
  children: ReactNode
}

function SystemCardShell({ section, title, icon, beaconOn = false, celebrate = false, onClick, children }: ShellProps) {
  return (
    <button
      type="button"
      data-section={section}
      onClick={onClick}
      className={[
        'mc-frame mc-glow-after relative overflow-hidden text-left w-full p-4 cursor-pointer',
        'transition-transform duration-200 hover:scale-[1.012] focus-visible:ring-2',
        'focus-visible:ring-[var(--accent-section)]',
        beaconOn ? 'mc-glow-pulse' : '',
        celebrate ? 'mc-celebrate' : '',
      ].join(' ')}
    >
      <CornerBrackets />
      {celebrate && <span className="mc-celebrate-beam left-0" aria-hidden />}
      <header className="flex items-center justify-between mb-3">
        <span className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--accent-section)]">
          {icon}
          {title}
        </span>
        <Beacon color="var(--accent-section)" on={beaconOn} size={5} />
      </header>
      {children}
    </button>
  )
}

function Metric({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="leading-tight">
      <div className="text-[9.5px] uppercase tracking-[0.16em] text-[var(--color-text-muted)]">{label}</div>
      <div className="text-base font-semibold">{children}</div>
    </div>
  )
}

// ── Forge ────────────────────────────────────────────────────────────────────

export function ForgeSystemCard({ onNavigate }: { onNavigate: (t: TabId) => void }) {
  const { data } = useForgeState()
  const s = data?.summary
  const latest = data?.cycles?.[0]
  const passStamp = latest && latest.status === 'pass' ? `${latest.id ?? ''}|${latest.ts ?? ''}` : null
  const { celebrating } = useCelebration('forge-pass', passStamp)

  return (
    <SystemCardShell
      section="forge"
      title="Forge"
      icon={<GlyphFlame size={13} />}
      beaconOn={data?.status?.running === true}
      celebrate={celebrating}
      onClick={() => onNavigate('forge')}
    >
      <div className="display-num text-3xl mb-1">
        <AnimatedNumber value={s?.passes} />
        <span className="text-sm text-[var(--color-text-muted)] font-sans font-medium ml-2">passes</span>
      </div>
      <div className="grid grid-cols-3 gap-2 mt-3">
        <Metric label="cycles"><AnimatedNumber value={s?.cycles} /></Metric>
        <Metric label="pass rate"><span className="font-mono">{s?.pass_rate ?? '—'}</span></Metric>
        <Metric label="FDR bar">
          <AnimatedNumber value={s?.fdr_bar} format={(v) => v.toFixed(3)} />
        </Metric>
      </div>
      {/* FDR rising-bar mini meter */}
      <div className="mt-3 h-1.5 rounded-full bg-[var(--color-surface-alt)] overflow-hidden" aria-hidden>
        <div
          className="h-full rounded-full transition-[width] duration-700"
          style={{
            width: `${Math.min(100, (s?.fdr_bar ?? 0) * 100)}%`,
            background: 'linear-gradient(90deg, var(--mc-forge), var(--mc-forge-hot))',
            boxShadow: '0 0 8px color-mix(in srgb, var(--mc-forge) 60%, transparent)',
          }}
        />
      </div>
      {celebrating && (
        <div className="absolute right-4 bottom-3 text-[10px] font-mono font-bold text-[var(--mc-forge-hot)] mc-stamp">
          ★ NEW PASS
        </div>
      )}
    </SystemCardShell>
  )
}

// ── Paper Book ───────────────────────────────────────────────────────────────

export function PaperSystemCard({ onNavigate }: { onNavigate: (t: TabId) => void }) {
  const { data } = usePortfolioData()
  const equity = data?.summary?.equity ?? data?.account?.equity
  const todayPnl = data?.summary?.today_pnl
  const history = (data?.portfolio_history ?? []).slice(-60)
  const marketOpen = data?.market_clock?.is_open === true

  return (
    <SystemCardShell
      section="paper"
      title="Paper Book"
      icon={<GlyphBook size={13} />}
      beaconOn={marketOpen}
      onClick={() => onNavigate('portfolio')}
    >
      <div className="display-num text-3xl mb-1">
        <AnimatedNumber value={equity} format={fmtCcy} flashOnDelta />
      </div>
      <div className="grid grid-cols-2 gap-2 mt-3">
        <Metric label="today P&L">
          <AnimatedNumber
            value={todayPnl}
            format={fmtSignedCcy}
            flashOnDelta
            className={todayPnl != null ? (todayPnl >= 0 ? 'text-[var(--color-positive)]' : 'text-[var(--color-negative)]') : ''}
          />
        </Metric>
        <Metric label="positions">
          <AnimatedNumber value={data?.summary?.open_positions} />
          {data?.summary?.max_positions != null && (
            <span className="text-[var(--color-text-muted)] font-mono">/{data.summary.max_positions}</span>
          )}
        </Metric>
      </div>
      {history.length > 1 && (
        <div className="mt-3 h-10">
          <Chart
            kind="sparkline"
            drawIn
            data={{
              labels: history.map((p) => p.date ?? ''),
              datasets: [
                {
                  data: history.map((p) => p.equity ?? null),
                  borderColor: 'var(--mc-paper)',
                  borderWidth: 1.5,
                  pointRadius: 0,
                  fill: false,
                  tension: 0.3,
                },
              ],
            }}
          />
        </div>
      )}
    </SystemCardShell>
  )
}

// ── Live ─────────────────────────────────────────────────────────────────────

export function LiveSystemCard({ onNavigate }: { onNavigate: (t: TabId) => void }) {
  const { data } = useLiveState()
  const overall = data?.gates?.overall
  const anyStrategyGates = data?.gates?.per_strategy && Object.values(data.gates.per_strategy)[0]
  const trackStatus = anyStrategyGates?.track?.status

  return (
    <SystemCardShell
      section="live"
      title="Live Pipeline"
      icon={<GlyphSignal size={13} />}
      beaconOn={data?.kill_switch?.blocked === true}
      onClick={() => onNavigate('live')}
    >
      <div className="display-num text-3xl mb-1">
        <AnimatedNumber value={data?.portfolio?.total_equity} format={fmtCcy} flashOnDelta />
      </div>
      <div className="grid grid-cols-2 gap-2 mt-3">
        <Metric label="deployed">
          <AnimatedNumber value={data?.portfolio?.n_strategies ?? data?.deployed?.length} />
          <span className="text-[var(--color-text-muted)] font-mono text-xs ml-1">strategies</span>
        </Metric>
        <Metric label="track">
          <span className="font-mono text-sm uppercase">{trackStatus ?? '—'}</span>
        </Metric>
      </div>
      <div className="flex flex-wrap gap-1.5 mt-3">
        {data?.gates ? (
          <>
            <GateStatusPill label="G6" pass={anyStrategyGates?.slippage?.pass} detail="slippage vs 16 bps bar" />
            <GateStatusPill label="G7" pass={anyStrategyGates?.broker_errors?.pass} detail="broker error rate vs 1%" />
            <GateStatusPill label="TRK" pass={anyStrategyGates?.track?.pass} detail="track-vs-expectation" />
          </>
        ) : (
          <span className="text-[10px] font-mono text-[var(--color-text-muted)] tracking-[0.12em]">
            GO-LIVE GATES — AWAITING DATA
          </span>
        )}
        {overall?.pass === false && (
          <span className="text-[10px] font-mono text-[var(--color-negative)] tracking-[0.1em] self-center">
            {overall.n_fail}/{overall.n_strategies} failing
          </span>
        )}
      </div>
    </SystemCardShell>
  )
}
