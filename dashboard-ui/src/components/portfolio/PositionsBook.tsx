import { memo, useMemo, useState } from 'react'
import type { Position } from '../../api/types'
import { fmtCcy, fmtSignedCcy, fmtSignedPct, pnlClass } from '../../lib/format'
import { EmptyState } from '../shared/EmptyState'

/**
 * PositionsBook — grouped, dense replacement for the tile grid.
 *
 * With 100+ small positions (forge strategies deploy broad books), per-position tiles
 * require endless scrolling. This renders the book the way a trader reads one:
 *   - group by Strategy (default) or Sector, collapsible, aggregates in the header
 *   - dense mono table rows inside each group, sorted by |P&L| (movers first)
 *   - ticker filter for jumping to a name
 */

type GroupBy = 'strategy' | 'sector'
type SortBy = 'pnl' | 'value' | 'ticker'

const SORTS: Record<SortBy, (a: Position, b: Position) => number> = {
  pnl: (a, b) => Math.abs(b.unrealized_pnl ?? 0) - Math.abs(a.unrealized_pnl ?? 0),
  value: (a, b) => Math.abs(b.market_value ?? 0) - Math.abs(a.market_value ?? 0),
  ticker: (a, b) => (a.ticker ?? '').localeCompare(b.ticker ?? ''),
}

const STRATEGY_LABEL: Record<string, string> = { shared: 'Shared (multiple books)' }

function groupKey(p: Position, by: GroupBy): string {
  const v = by === 'strategy' ? p.strategy : p.sector
  return v && v !== 'Unknown' ? v : 'Unattributed'
}

interface GroupAgg {
  name: string
  rows: Position[]
  value: number      // gross market value
  pnl: number
  longs: number
  shorts: number
}

function aggregate(positions: Position[], by: GroupBy, sort: SortBy): GroupAgg[] {
  const map = new Map<string, GroupAgg>()
  for (const p of positions) {
    const k = groupKey(p, by)
    let g = map.get(k)
    if (!g) { g = { name: k, rows: [], value: 0, pnl: 0, longs: 0, shorts: 0 }; map.set(k, g) }
    g.rows.push(p)
    g.value += Math.abs(p.market_value ?? 0)
    g.pnl += p.unrealized_pnl ?? 0
    if ((p.shares ?? 0) < 0) g.shorts += 1; else g.longs += 1
  }
  const groups = Array.from(map.values())
  for (const g of groups) g.rows.sort(SORTS[sort])
  // biggest groups first; Unattributed last
  groups.sort((a, b) =>
    (a.name === 'Unattributed' ? 1 : 0) - (b.name === 'Unattributed' ? 1 : 0) || b.value - a.value)
  return groups
}

function SegButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-0.5 rounded text-[10px] uppercase tracking-wider font-semibold transition-colors ${
        active
          ? 'bg-[var(--color-accent)]/15 text-[var(--color-accent)]'
          : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]'
      }`}
    >
      {children}
    </button>
  )
}

function Row({ p, showSector }: { p: Position; showSector: boolean }) {
  const short = (p.shares ?? 0) < 0
  const pnl = p.unrealized_pnl ?? 0
  return (
    <tr data-testid="position-row" className="border-b border-[var(--color-border)]/30 hover:bg-[var(--color-border)]/15">
      <td className="py-1 pr-3">
        <span className="font-semibold text-[var(--color-text)]">{p.ticker ?? '\u2014'}</span>
        {short && (
          <span className="ml-1.5 text-[9px] uppercase tracking-wider px-1 py-px rounded bg-[var(--color-amber)]/15 text-[var(--color-amber)]">
            short
          </span>
        )}
      </td>
      {showSector && (
        <td className="py-1 pr-3 text-[var(--color-text-muted)] truncate max-w-[110px]" title={p.sector ?? ''}>
          {p.sector && p.sector !== 'Unknown' ? p.sector : '\u2014'}
        </td>
      )}
      <td className="py-1 pr-3 text-right tabular-nums text-[var(--color-text-muted)]">{p.shares ?? 0}</td>
      <td className="py-1 pr-3 text-right tabular-nums">{fmtCcy(p.market_value)}</td>
      <td className="py-1 pr-3 text-right tabular-nums text-[var(--color-text-muted)] hidden md:table-cell">
        {fmtCcy(p.entry_price)}
      </td>
      <td className="py-1 pr-3 text-right tabular-nums hidden md:table-cell">{fmtCcy(p.current_price)}</td>
      <td className={`py-1 pr-3 text-right tabular-nums font-semibold ${pnlClass(pnl)}`}>{fmtSignedCcy(pnl)}</td>
      <td className={`py-1 pr-3 text-right tabular-nums ${pnlClass(pnl)}`}>{fmtSignedPct(p.unrealized_pnl_pct)}</td>
      <td className={`py-1 text-right tabular-nums hidden sm:table-cell ${pnlClass(p.intraday_pnl)}`}>
        {p.intraday_pnl ? fmtSignedCcy(p.intraday_pnl) : '\u2014'}
      </td>
    </tr>
  )
}

function Group({ g, showSector, defaultOpen }: { g: GroupAgg; showSector: boolean; defaultOpen: boolean }) {
  return (
    <details open={defaultOpen} className="group/pos mc-frame rounded-xl overflow-hidden">
      <summary className="flex items-center gap-3 px-3 py-2 cursor-pointer list-none select-none hover:bg-[var(--color-border)]/15">
        <svg className="w-3 h-3 shrink-0 text-[var(--color-text-muted)] transition-transform duration-200 group-open/pos:rotate-90"
             fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        <span className="font-mono font-semibold text-sm truncate">{STRATEGY_LABEL[g.name] ?? g.name}</span>
        <span className="text-[10px] text-[var(--color-text-muted)] tabular-nums shrink-0">
          {g.longs > 0 && `${g.longs}L`}{g.longs > 0 && g.shorts > 0 && ' · '}{g.shorts > 0 && `${g.shorts}S`}
        </span>
        <span className="ml-auto flex items-center gap-4 font-mono text-xs tabular-nums shrink-0">
          <span className="text-[var(--color-text-muted)]">{fmtCcy(g.value)}</span>
          <span className={`font-semibold ${pnlClass(g.pnl)}`}>{fmtSignedCcy(g.pnl)}</span>
        </span>
      </summary>
      <div className="overflow-x-auto border-t border-[var(--color-border)]/50">
        <table className="w-full text-xs font-mono">
          <thead>
            <tr className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] border-b border-[var(--color-border)]/50">
              <th className="text-left font-semibold py-1.5 pr-3 pl-3">Ticker</th>
              {showSector && <th className="text-left font-semibold py-1.5 pr-3">Sector</th>}
              <th className="text-right font-semibold py-1.5 pr-3">Qty</th>
              <th className="text-right font-semibold py-1.5 pr-3">Value</th>
              <th className="text-right font-semibold py-1.5 pr-3 hidden md:table-cell">Entry</th>
              <th className="text-right font-semibold py-1.5 pr-3 hidden md:table-cell">Last</th>
              <th className="text-right font-semibold py-1.5 pr-3">P&L</th>
              <th className="text-right font-semibold py-1.5 pr-3">%</th>
              <th className="text-right font-semibold py-1.5 hidden sm:table-cell">Today</th>
            </tr>
          </thead>
          <tbody className="[&>tr>td:first-child]:pl-3">
            {g.rows.map((p, i) => <Row key={p.ticker ?? i} p={p} showSector={showSector} />)}
          </tbody>
        </table>
      </div>
    </details>
  )
}

function PositionsBookInner({ positions }: { positions: Position[] }) {
  const [groupBy, setGroupBy] = useState<GroupBy>('strategy')
  const [sortBy, setSortBy] = useState<SortBy>('pnl')
  const [filter, setFilter] = useState('')

  const filtered = useMemo(() => {
    const q = filter.trim().toUpperCase()
    return q ? positions.filter((p) => (p.ticker ?? '').toUpperCase().includes(q)) : positions
  }, [positions, filter])

  const groups = useMemo(() => aggregate(filtered, groupBy, sortBy), [filtered, groupBy, sortBy])

  const longs = positions.filter((p) => (p.shares ?? 0) >= 0).length
  const shorts = positions.length - longs

  return (
    <div>
      {/* Header: count + long/short split + controls */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 mb-3">
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold">
          OPEN POSITIONS ({positions.length})
          {shorts > 0 && (
            <span className="ml-2 normal-case tracking-normal font-mono">
              {longs} long · {shorts} short
            </span>
          )}
        </div>
        <div className="ml-auto flex items-center gap-1 flex-wrap">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={'filter ticker\u2026'}
            className="bg-transparent border border-[var(--color-border)] rounded px-2 py-0.5 text-xs font-mono w-28
                       placeholder:text-[var(--color-text-muted)]/60 focus:outline-none focus:border-[var(--color-accent)]/50"
          />
          <span className="mx-1 text-[var(--color-border)]">|</span>
          <SegButton active={groupBy === 'strategy'} onClick={() => setGroupBy('strategy')}>Strategy</SegButton>
          <SegButton active={groupBy === 'sector'} onClick={() => setGroupBy('sector')}>Sector</SegButton>
          <span className="mx-1 text-[var(--color-border)]">|</span>
          <SegButton active={sortBy === 'pnl'} onClick={() => setSortBy('pnl')}>Movers</SegButton>
          <SegButton active={sortBy === 'value'} onClick={() => setSortBy('value')}>Size</SegButton>
          <SegButton active={sortBy === 'ticker'} onClick={() => setSortBy('ticker')}>{'A\u2013Z'}</SegButton>
        </div>
      </div>

      {positions.length === 0 ? (
        <EmptyState icon="\u25a1" heading="No open positions"
                    description="Positions will appear here when the portfolio is active." />
      ) : groups.length === 0 ? (
        <div className="text-xs text-[var(--color-text-muted)] font-mono px-1 py-4">
          No tickers match &ldquo;{filter}&rdquo;
        </div>
      ) : (
        <div className="space-y-2 stagger-pop">
          {groups.map((g) => (
            <Group key={g.name} g={g} showSector={groupBy !== 'sector'}
                   defaultOpen={groups.length <= 4 || g.rows.length >= 10} />
          ))}
        </div>
      )}
    </div>
  )
}

export const PositionsBook = memo(PositionsBookInner)
