import { useLiveState } from '../../api/queries'
import type { LiveDeployed, LiveDailyResult, LivePortfolio } from '../../api/queries'
import { Skeleton } from '../layout/Skeleton'
import { SectionBoundary } from '../layout/SectionBoundary'
import { fmtCcy, fmtSignedCcy } from '../../lib/format'
import { AnimatedNumber } from '../ui/AnimatedNumber'
import { HudPanel, Beacon, StreamDivider } from '../ui/hud'
import { GlyphCheck, GlyphX, GlyphShield, GlyphSignal } from '../ui/glyphs'
import { GatesPanel } from './GatesPanel'

const STATE_COLOR: Record<string, string> = {
  shadow: 'var(--color-text-muted)',
  canary: 'var(--color-warning)',
  live: 'var(--mc-live-hot)',
}

function pnlColor(v?: number | null): string {
  if (v == null) return 'var(--color-text-muted)'
  return v >= 0 ? 'var(--color-positive)' : 'var(--color-negative)'
}

function fmtPct(v?: number | null): string {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`
}

// ── Kill switch ──────────────────────────────────────────────────────────────

function KillSwitchBanner({ blocked, reason, layer }: { blocked: boolean; reason?: string | null; layer?: string | null }) {
  const color = blocked ? 'var(--mc-live)' : 'var(--color-positive)'
  return (
    <HudPanel brackets glow glowPulse={blocked} className="overflow-hidden">
      <div className="flex items-center gap-4">
        <Beacon color={color} on size={9} />
        <div className="leading-tight min-w-0">
          <div className="display-num text-lg sm:text-xl" style={{ color }}>
            KILL-SWITCH: {blocked ? 'HALTED' : 'CLEAR'}
          </div>
          <div className="text-[10px] uppercase tracking-[0.2em] text-[var(--color-text-muted)] flex items-center gap-1 truncate">
            <GlyphShield size={10} />
            {blocked ? `${layer ? `[${layer}] ` : ''}${reason ?? ''}` : 'all layers green — orders flow'}
          </div>
        </div>
      </div>
    </HudPanel>
  )
}

// ── Portfolio rollup ─────────────────────────────────────────────────────────

function PortfolioHeader({ p }: { p: LivePortfolio }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
      <Tile label="Book Equity">
        <AnimatedNumber value={p.total_equity} format={fmtCcy} flashOnDelta className="display-num text-base" />
      </Tile>
      <Tile label="Capital Base">
        <AnimatedNumber value={p.total_capital_base} format={fmtCcy} className="text-base font-semibold text-[var(--color-text-muted)]" />
      </Tile>
      <Tile label="P&L">
        <AnimatedNumber
          value={p.total_pnl}
          format={fmtSignedCcy}
          flashOnDelta
          className="text-base font-semibold"
        />
      </Tile>
      <Tile label="Return">
        <span className="text-base font-mono font-semibold" style={{ color: pnlColor(p.total_return) }}>
          {fmtPct(p.total_return)}
        </span>
      </Tile>
      <Tile label="Strategies">
        <span className="text-base font-mono font-semibold">
          {p.n_tracked}<span className="text-[var(--color-text-muted)]">/{p.n_strategies} tracked</span>
        </span>
      </Tile>
    </div>
  )
}

function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mc-frame px-3 py-2">
      <div className="text-[9.5px] uppercase tracking-[0.16em] text-[var(--color-text-muted)] mb-0.5">{label}</div>
      <div className="tabular">{children}</div>
    </div>
  )
}

// ── Deployed strategies ──────────────────────────────────────────────────────

function MiniSparkline({ curve, base }: { curve: { equity?: number }[]; base?: number | null }) {
  const pts = curve.map((c) => c.equity).filter((e): e is number => e != null)
  if (pts.length < 2) return <span className="text-[10px] text-[var(--color-text-muted)]">—</span>
  const w = 96
  const h = 24
  const min = Math.min(...pts)
  const max = Math.max(...pts)
  const span = max - min || 1
  const path = pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${((i / (pts.length - 1)) * w).toFixed(1)},${(h - ((p - min) / span) * h).toFixed(1)}`)
    .join(' ')
  const up = base != null ? pts[pts.length - 1] >= base : pts[pts.length - 1] >= pts[0]
  const stroke = up ? 'var(--color-positive)' : 'var(--color-negative)'
  return (
    <svg width={w} height={h} className="inline-block align-middle">
      <path d={path} fill="none" stroke={stroke} strokeWidth="1.5"
        style={{ filter: `drop-shadow(0 0 3px ${stroke})` }} />
    </svg>
  )
}

function DeployedTable({ rows }: { rows: LiveDeployed[] }) {
  if (!rows.length) {
    return (
      <div className="text-sm text-[var(--color-text-muted)] py-6 text-center">
        No strategies deployed. The forge promotes a PASS into shadow here; real capital is gated on
        forward-paper evidence + the AUM floor (board 2026-06-09).
      </div>
    )
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead className="text-[var(--color-text-muted)] uppercase tracking-wider text-[10px]">
          <tr className="border-b border-[var(--color-border)]">
            <th className="text-left py-2 pr-4">Strategy</th>
            <th className="text-left py-2 pr-4">State</th>
            <th className="text-right py-2 pr-4">Equity</th>
            <th className="text-right py-2 pr-4">Cum Ret</th>
            <th className="text-right py-2 pr-4">Last Day</th>
            <th className="text-right py-2 pr-4">Days</th>
            <th className="text-right py-2 pr-4">Pos</th>
            <th className="text-right py-2 pr-4">Sharpe R/E</th>
            <th className="text-left py-2 pr-4">Curve</th>
            <th className="text-left py-2">Appr</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const b = s.book
            const stateColor = STATE_COLOR[s.state] ?? 'var(--color-text)'
            return (
              <tr key={s.name} className="border-b border-[var(--color-border)]/40 transition-colors hover:bg-[var(--color-surface-alt)]/40">
                <td className="py-2 pr-4 text-[var(--color-text)] font-semibold">{s.name}</td>
                <td className="py-2 pr-4">
                  <span className="inline-flex items-center gap-1.5" style={{ color: stateColor }}>
                    <Beacon color={stateColor} on={s.state !== 'shadow'} size={3.5} />
                    {s.state}/{s.broker}
                  </span>
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {b?.book_equity != null ? fmtCcy(b.book_equity) : fmtCcy(s.capital)}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums" style={{ color: pnlColor(b?.cum_return) }}>
                  {fmtPct(b?.cum_return)}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums" style={{ color: pnlColor(b?.last_return) }}>
                  {fmtPct(b?.last_return)}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-[var(--color-text-muted)]">{b?.days_tracked ?? 0}</td>
                <td className="py-2 pr-4 text-right tabular-nums text-[var(--color-text-muted)]">{b?.n_positions ?? '—'}</td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {b?.realized_sharpe != null ? b.realized_sharpe.toFixed(2) : '—'}
                  <span className="text-[var(--color-text-muted)]"> / {s.expectation?.sharpe?.toFixed(2) ?? '—'}</span>
                </td>
                <td className="py-2 pr-4"><MiniSparkline curve={b?.equity_curve ?? []} base={b?.capital_base} /></td>
                <td className="py-2">
                  {s.approved
                    ? <GlyphCheck size={12} className="text-[var(--color-positive)]" />
                    : <span className="text-[var(--color-text-muted)]">—</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ── Daily wire ───────────────────────────────────────────────────────────────

function DailyResults({ results }: { results: LiveDailyResult[] }) {
  if (!results.length) {
    return <div className="text-sm text-[var(--color-text-muted)] py-4 text-center">No runs in the latest report.</div>
  }
  return (
    <ul className="stagger space-y-1.5">
      {results.map((r) => (
        <li key={r.name} className="animate-in flex items-center gap-2.5 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-xs font-mono">
          {r.blocked
            ? <GlyphX size={12} className="text-[var(--color-negative)] shrink-0" />
            : r.track_status === 'diverging' || r.error
              ? <GlyphX size={12} className="text-[var(--color-warning)] shrink-0" />
              : <GlyphCheck size={12} className="text-[var(--color-positive)] shrink-0" />}
          <span className="text-[var(--color-text)] font-semibold">{r.name}</span>
          <span className="text-[var(--color-text-muted)] truncate">
            [{r.state}/{r.broker}] orders={r.n_orders} exec={r.executed} dry={String(r.dry_run)} track={r.track_status ?? '—'}
            {r.error ? `  err=${r.error}` : ''}
          </span>
          {r.awaiting_approval && (
            <span className="ml-auto shrink-0 text-[10px] font-bold tracking-[0.08em] text-[var(--color-warning)]">
              AWAITING APPROVAL
            </span>
          )}
          {r.blocked && (
            <span className="ml-auto shrink-0 text-[10px] font-bold tracking-[0.08em] text-[var(--color-negative)]">
              BLOCKED {r.blocked}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

// ── Tab ──────────────────────────────────────────────────────────────────────

export function LiveTab() {
  const { data } = useLiveState()
  if (!data) return <Skeleton className="h-64" />

  return (
    <div className="space-y-4 md:space-y-5 stagger-pop" data-section="live">
      <SectionBoundary title="Kill switch">
        <KillSwitchBanner {...data.kill_switch} />
      </SectionBoundary>

      {data.portfolio && (
        <SectionBoundary title="Paper Portfolio">
          <PortfolioHeader p={data.portfolio} />
        </SectionBoundary>
      )}

      <SectionBoundary title="Go-live gates">
        <GatesPanel gates={data.gates} />
      </SectionBoundary>

      <SectionBoundary title="Deployed Strategies">
        <HudPanel title={<span className="flex items-center gap-1.5"><GlyphSignal size={12} /> Deployed Strategies</span>}>
          <DeployedTable rows={data.deployed} />
        </HudPanel>
      </SectionBoundary>

      <SectionBoundary title="Latest shadow run">
        <HudPanel
          title={`Latest Shadow Run${data.daily ? ` · ${data.daily.date} (${data.daily.mode})` : ''}`}
        >
          <StreamDivider className="mb-3" />
          {data.daily ? <DailyResults results={data.daily.results} /> : (
            <div className="text-sm text-[var(--color-text-muted)] py-4 text-center">No daily report yet.</div>
          )}
        </HudPanel>
      </SectionBoundary>
    </div>
  )
}
