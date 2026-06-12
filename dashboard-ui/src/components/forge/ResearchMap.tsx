/**
 * ResearchMap — the wiki rendered as a family-swimlane lineage map.
 *
 * Lanes = premium families (illiquidity, value×momentum, carry…), ordered by activity.
 * Nodes = hypotheses, chronological within their lane; queued ghosts pinned to the
 * right edge ("what's coming"). Edges = lineage: refine (solid), orthogonal (dashed),
 * crossover (orange, joins two lanes), pairs_with (faint dotted relation).
 * Node size ∝ DSR; purple ring = current MAP-Elites pool occupant.
 *
 * Pure SVG — ~80 nodes needs no graph library. Click a node for the detail drawer.
 */
import { useMemo, useState } from 'react'
import { useResearchMap } from '../../api/forge-queries'
import type { MapEdge, MapNode, MapNodeStatus, ResearchMapData } from '../../api/map-types'
import { Skeleton } from '../layout/Skeleton'
import { C, Card, fmtMetric } from './shared'

// ── palette ──────────────────────────────────────────────────────────────────
const NODE_COLOR: Record<MapNodeStatus, string> = {
  pass: C.gold, near_miss: C.ember, fail: '#7f1d1d', closed: '#3f3f46',
  queued: '#52525b', claimed: '#6366f1', other: C.iron,
}
const STATUS_LABEL: Record<MapNodeStatus, string> = {
  pass: 'PASS', near_miss: 'NEAR-MISS', fail: 'FAIL', closed: 'CLOSED',
  queued: 'QUEUED', claimed: 'RUNNING', other: 'OTHER',
}
const EDGE_STYLE: Record<MapEdge['kind'], { stroke: string; dash?: string; width: number; opacity: number }> = {
  refine: { stroke: '#a1a1aa', width: 1.4, opacity: 0.55 },
  orthogonal: { stroke: C.indigo, dash: '6 4', width: 1.4, opacity: 0.7 },
  crossover: { stroke: C.hot, width: 2.2, opacity: 0.85 },
  pairs_with: { stroke: '#52525b', dash: '2 5', width: 1, opacity: 0.35 },
}

// ── layout ───────────────────────────────────────────────────────────────────
const GAP = 58            // px between nodes in a lane
const LANE_H = 72         // lane row height
const X0 = 16             // left padding inside the scroll area (labels are HTML, outside SVG)
const R_MIN = 6, R_MAX = 12

function radius(n: MapNode): number {
  const d = n.dsr ?? n.metrics?.dsr
  if (d == null) return R_MIN + 1
  return R_MIN + Math.max(0, Math.min(1, d)) * (R_MAX - R_MIN)
}

interface Pos { x: number; y: number; n: MapNode }

function layout(data: ResearchMapData) {
  const lanes = data.lanes
  const byLane = new Map<string, MapNode[]>()
  for (const n of data.nodes) {
    if (!byLane.has(n.lane)) byLane.set(n.lane, [])
    byLane.get(n.lane)!.push(n)
  }
  let maxLen = 0
  const pos = new Map<string, Pos>()
  lanes.forEach((lane, li) => {
    const ns = (byLane.get(lane.id) ?? []).slice().sort((a, b) => {
      const ga = a.status === 'queued' || a.status === 'claimed' ? 1 : 0
      const gb = b.status === 'queued' || b.status === 'claimed' ? 1 : 0
      if (ga !== gb) return ga - gb               // ghosts last (right edge)
      return (a.ts ?? a.date ?? '').localeCompare(b.ts ?? b.date ?? '') || a.id.localeCompare(b.id)
    })
    maxLen = Math.max(maxLen, ns.length)
    ns.forEach((n, i) => pos.set(n.id, { x: X0 + 24 + i * GAP, y: li * LANE_H + LANE_H / 2, n }))
  })
  return { pos, width: X0 + 48 + maxLen * GAP, height: lanes.length * LANE_H }
}

function edgePath(a: Pos, b: Pos): string {
  if (a.y === b.y) {
    // same lane: arc above the row so chains of refinements stay readable
    const lift = Math.min(26, 8 + Math.abs(b.x - a.x) / 8)
    return `M ${a.x} ${a.y - 4} C ${a.x} ${a.y - lift}, ${b.x} ${b.y - lift}, ${b.x} ${b.y - 4}`
  }
  const mx = (a.x + b.x) / 2
  return `M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`
}

// ── drawer ───────────────────────────────────────────────────────────────────
function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-2 py-1.5 rounded bg-[var(--color-surface-alt)]">
      <div className="text-[9px] uppercase tracking-wide text-[var(--color-text-muted)]">{label}</div>
      <div className="text-xs font-bold tabular-nums">{value}</div>
    </div>
  )
}

function NodeDrawer({ node, onClose }: { node: MapNode; onClose: () => void }) {
  const m = node.metrics ?? {}
  return (
    <Card brackets className="p-4 space-y-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wide"
              style={{ background: `${NODE_COLOR[node.status]}26`, color: NODE_COLOR[node.status] === '#7f1d1d' ? '#f87171' : NODE_COLOR[node.status] }}>
              {STATUS_LABEL[node.status]}
            </span>
            {node.elite && (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wide"
                style={{ background: 'rgba(168,85,247,0.15)', color: '#c084fc' }}>★ ELITE POOL</span>
            )}
            {node.tier && <span className="text-[10px] text-[var(--color-text-muted)]">tier {node.tier}</span>}
          </div>
          <div className="font-bold mt-1 leading-snug">{node.title}</div>
          <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5">
            {node.family || node.lane}{node.markets.length > 0 && <> · {node.markets.join(', ')}</>}
            {node.date && <> · {node.date}</>}
            {node.arm && <> · arm: <span className="text-[var(--color-text)]">{node.arm}</span></>}
            {node.agent && <> · {node.agent}</>}
          </div>
        </div>
        <button onClick={onClose} className="text-[var(--color-text-muted)] hover:text-[var(--color-text)] text-lg leading-none px-1" aria-label="close">×</button>
      </div>

      {(m.search_sharpe != null || m.holdout_sharpe != null || node.dsr != null) && (
        <div className="grid grid-cols-3 sm:grid-cols-6 gap-1.5">
          <Metric label="DSR" value={fmtMetric(node.dsr ?? m.dsr)} />
          <Metric label="search Sh" value={fmtMetric(m.search_sharpe)} />
          <Metric label="holdout Sh" value={fmtMetric(m.holdout_sharpe)} />
          <Metric label="full Sh" value={fmtMetric(m.full_sharpe)} />
          <Metric label="maxDD" value={m.maxdd != null ? `${(m.maxdd * 100).toFixed(0)}%` : '—'} />
          <Metric label="bar @ test" value={fmtMetric(node.bar_at_test, 'ratio')} />
        </div>
      )}

      {node.prereg && (
        <div>
          <div className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)] mb-1">Pre-registration</div>
          <p className="text-xs text-[var(--color-text-muted)] leading-relaxed whitespace-pre-wrap">{node.prereg}</p>
        </div>
      )}
      {node.page && (
        <div className="text-[11px] text-[var(--color-text-muted)]">
          wiki: <code className="text-xs">experiments/{node.page}.md</code>
        </div>
      )}
    </Card>
  )
}

// ── main ─────────────────────────────────────────────────────────────────────
const FILTERS: MapNodeStatus[] = ['pass', 'near_miss', 'fail', 'closed', 'queued']

export function ResearchMap() {
  const q = useResearchMap()
  const [selected, setSelected] = useState<string | null>(null)
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null)
  const [statusFilter, setStatusFilter] = useState<Set<MapNodeStatus>>(new Set())
  const [search, setSearch] = useState('')
  const [focusLineage, setFocusLineage] = useState(false)

  const data = q.data
  const lay = useMemo(() => (data ? layout(data) : null), [data])

  // lineage neighborhood of the selected node (ancestors + descendants, 1 hop transitive)
  const lineageIds = useMemo(() => {
    if (!data || !selected || !focusLineage) return null
    const ids = new Set<string>([selected])
    let grew = true
    while (grew) {
      grew = false
      for (const e of data.edges) {
        if (e.kind === 'pairs_with') continue
        if (ids.has(e.source) && !ids.has(e.target)) { ids.add(e.target); grew = true }
        if (ids.has(e.target) && !ids.has(e.source)) { ids.add(e.source); grew = true }
      }
    }
    return ids
  }, [data, selected, focusLineage])

  if (q.isLoading && !data) {
    return <div className="space-y-4"><Skeleton className="h-14" /><Skeleton className="h-96" /></div>
  }
  if (q.isError || !data || !lay) {
    return (
      <div className="p-6 text-center text-sm text-[var(--color-negative)] bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl">
        Couldn’t load the research map — <code className="text-xs">/api/forge/map</code>
      </div>
    )
  }

  const s = data.stats
  const searchLower = search.trim().toLowerCase()
  const visible = (n: MapNode): boolean => {
    if (statusFilter.size > 0 && !statusFilter.has(n.status === 'claimed' ? 'queued' : n.status)) return false
    if (searchLower && !`${n.title} ${n.id} ${n.family}`.toLowerCase().includes(searchLower)) return false
    if (lineageIds && !lineageIds.has(n.id)) return false
    return true
  }
  const dim = (n: MapNode) => (visible(n) ? 1 : 0.12)
  const selectedNode = selected ? data.nodes.find((n) => n.id === selected) ?? null : null
  const hoverNode = hover ? data.nodes.find((n) => n.id === hover.id) ?? null : null

  const toggleFilter = (f: MapNodeStatus) =>
    setStatusFilter((prev) => {
      const next = new Set(prev)
      if (next.has(f)) next.delete(f); else next.add(f)
      return next
    })

  return (
    <div className="space-y-4" data-section="research-map">
      {/* ── stats strip ── */}
      <Card brackets className="px-5 py-3.5">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="text-2xl">🗺️</span>
            <div>
              <div className="text-sm font-bold">Research Map</div>
              <div className="text-[11px] text-[var(--color-text-muted)]">
                every hypothesis tested, queued, and how they descend from each other
              </div>
            </div>
          </div>
          <div className="grid grid-cols-4 sm:grid-cols-8 gap-2 text-center">
            {[
              ['tested', s.experiments, undefined],
              ['pass', s.passes, C.gold],
              ['near-miss', s.near_misses, C.ember],
              ['fail', s.fails, '#f87171'],
              ['queued', s.queued, C.indigo],
              ['FDR bar', s.fdr_bar?.toFixed(3), C.indigo],
              ['families', s.families_burned, undefined],
              ['elite cells', s.elite_cells, '#c084fc'],
            ].map(([label, value, color]) => (
              <div key={label as string} className="px-2.5 py-1.5 rounded-lg bg-[var(--color-surface-alt)]">
                <div className="text-[9px] uppercase tracking-wide text-[var(--color-text-muted)]">{label}</div>
                <div className="text-sm font-bold tabular-nums" style={{ color: (color as string) || 'var(--color-text)' }}>{value}</div>
              </div>
            ))}
          </div>
        </div>
      </Card>

      {/* ── filter bar ── */}
      <div className="flex flex-wrap items-center gap-2 px-1">
        {FILTERS.map((f) => {
          const active = statusFilter.has(f)
          const col = NODE_COLOR[f] === '#7f1d1d' ? '#f87171' : NODE_COLOR[f]
          return (
            <button key={f} onClick={() => toggleFilter(f)}
              className="px-2.5 py-1 rounded-full text-[11px] font-bold tracking-wide border transition-colors"
              style={{
                borderColor: active ? col : 'var(--color-border)',
                color: active ? col : 'var(--color-text-muted)',
                background: active ? `${col}1a` : 'transparent',
              }}>
              {STATUS_LABEL[f]}
            </button>
          )
        })}
        <input
          value={search} onChange={(e) => setSearch(e.target.value)} placeholder="search hypotheses…"
          className="px-3 py-1 rounded-full text-[12px] bg-[var(--color-surface-alt)] border border-[var(--color-border)] outline-none focus:border-[var(--color-text-muted)] w-48"
        />
        {selected && (
          <button onClick={() => setFocusLineage((v) => !v)}
            className="px-2.5 py-1 rounded-full text-[11px] font-bold tracking-wide border transition-colors"
            style={{
              borderColor: focusLineage ? '#c084fc' : 'var(--color-border)',
              color: focusLineage ? '#c084fc' : 'var(--color-text-muted)',
              background: focusLineage ? 'rgba(168,85,247,0.1)' : 'transparent',
            }}>
            ⤳ lineage only
          </button>
        )}
        <div className="ml-auto text-[10px] text-[var(--color-text-muted)] hidden md:block">
          {s.edges} edges ({s.explicit_edges} explicit) · node size = DSR · ring = elite pool
        </div>
      </div>

      {/* ── the map ── */}
      <Card className="relative overflow-hidden">
        <div className="flex">
          {/* lane labels — sticky HTML column */}
          <div className="shrink-0 w-44 border-r border-[var(--color-border)] bg-[var(--color-surface)] z-10">
            {data.lanes.map((lane) => (
              <div key={lane.id} className="px-3 flex flex-col justify-center" style={{ height: LANE_H }}>
                <div className="text-xs font-bold leading-tight truncate" title={lane.label}>{lane.label}</div>
                <div className="text-[10px] text-[var(--color-text-muted)] tabular-nums flex gap-1.5 mt-0.5">
                  <span>{lane.total}</span>
                  {lane.pass > 0 && <span style={{ color: C.gold }}>{lane.pass}✓</span>}
                  {lane.near_miss > 0 && <span style={{ color: C.ember }}>{lane.near_miss}≈</span>}
                  {lane.queued > 0 && <span style={{ color: C.indigo }}>{lane.queued}⏳</span>}
                </div>
                {lane.premia_note && (
                  <div className="text-[9px] text-[var(--color-text-muted)] truncate" title={lane.premia_note}>
                    {lane.premia_note}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* scrollable graph */}
          <div className="overflow-x-auto grow">
            <svg width={lay.width} height={lay.height} role="img" aria-label="Research lineage map">
              {/* lane separators */}
              {data.lanes.map((lane, i) => (
                <line key={lane.id} x1={0} x2={lay.width} y1={(i + 1) * LANE_H} y2={(i + 1) * LANE_H}
                  stroke="var(--color-border)" strokeWidth={1} opacity={0.6} />
              ))}

              {/* edges under nodes */}
              {data.edges.map((e, i) => {
                const a = lay.pos.get(e.source), b = lay.pos.get(e.target)
                if (!a || !b) return null
                const st = EDGE_STYLE[e.kind]
                const lit = visible(a.n) && visible(b.n)
                return (
                  <path key={i} d={edgePath(a, b)} fill="none"
                    stroke={st.stroke} strokeWidth={st.width} strokeDasharray={st.dash}
                    opacity={lit ? st.opacity * (e.inferred ? 0.75 : 1) : 0.05} />
                )
              })}

              {/* nodes */}
              {[...lay.pos.values()].map(({ x, y, n }) => {
                const r = radius(n)
                const ghost = n.status === 'queued' || n.status === 'claimed'
                const isSel = n.id === selected
                return (
                  <g key={n.id} transform={`translate(${x},${y})`} opacity={dim(n)}
                    className="cursor-pointer"
                    onClick={() => { setSelected(isSel ? null : n.id); if (isSel) setFocusLineage(false) }}
                    onMouseEnter={() => setHover({ id: n.id, x, y })}
                    onMouseLeave={() => setHover(null)}>
                    {n.elite && <circle r={r + 3.5} fill="none" stroke="#c084fc" strokeWidth={1.6} opacity={0.9} />}
                    {isSel && <circle r={r + 6} fill="none" stroke="var(--color-text)" strokeWidth={1} strokeDasharray="3 3" />}
                    <circle r={r}
                      fill={ghost ? 'transparent' : NODE_COLOR[n.status]}
                      stroke={ghost ? NODE_COLOR[n.status] : 'rgba(0,0,0,0.4)'}
                      strokeWidth={ghost ? 1.6 : 0.8}
                      strokeDasharray={ghost ? '3 2' : undefined} />
                    {n.status === 'pass' && <circle r={r + 1.5} fill="none" stroke={C.gold} strokeWidth={0.8} opacity={0.5} />}
                  </g>
                )
              })}
            </svg>
          </div>
        </div>

        {/* hover tooltip */}
        {hover && hoverNode && (
          <div className="pointer-events-none absolute z-20 px-3 py-2 rounded-lg text-xs max-w-72 shadow-xl border border-[var(--color-border)] bg-[var(--color-surface)]"
            style={{ left: Math.min(hover.x + 192, window.innerWidth - 340), ...tooltipPos(hover, lay.height) }}>
            <div className="font-bold leading-snug">{hoverNode.title}</div>
            <div className="text-[var(--color-text-muted)] mt-0.5">
              <span style={{ color: NODE_COLOR[hoverNode.status] === '#7f1d1d' ? '#f87171' : NODE_COLOR[hoverNode.status] }}>
                {STATUS_LABEL[hoverNode.status]}
              </span>
              {hoverNode.dsr != null && <> · DSR {hoverNode.dsr.toFixed(2)}</>}
              {hoverNode.metrics?.holdout_sharpe != null && <> · holdout {hoverNode.metrics.holdout_sharpe.toFixed(2)}</>}
              {hoverNode.date && <> · {hoverNode.date}</>}
            </div>
          </div>
        )}
      </Card>

      {/* ── legend ── */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-2 text-[10px] text-[var(--color-text-muted)]">
        <span className="flex items-center gap-1"><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="#a1a1aa" strokeWidth="1.4" /></svg>refine</span>
        <span className="flex items-center gap-1"><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke={C.indigo} strokeWidth="1.4" strokeDasharray="6 4" /></svg>orthogonal</span>
        <span className="flex items-center gap-1"><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke={C.hot} strokeWidth="2.2" /></svg>crossover</span>
        <span className="flex items-center gap-1"><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="#52525b" strokeWidth="1" strokeDasharray="2 5" /></svg>pairs-with</span>
        <span className="flex items-center gap-1"><svg width="14" height="14"><circle cx="7" cy="7" r="5" fill="none" stroke="#52525b" strokeWidth="1.6" strokeDasharray="3 2" /></svg>queued</span>
        <span className="flex items-center gap-1"><svg width="14" height="14"><circle cx="7" cy="7" r="4" fill="#71717a" /><circle cx="7" cy="7" r="6.4" fill="none" stroke="#c084fc" strokeWidth="1.4" /></svg>elite pool</span>
        <span className="ml-auto">generated {new Date(data.generated_at).toLocaleTimeString()}</span>
      </div>

      {/* ── detail drawer ── */}
      {selectedNode && <NodeDrawer node={selectedNode} onClose={() => { setSelected(null); setFocusLineage(false) }} />}
    </div>
  )
}

/** Keep the tooltip inside the card vertically. */
function tooltipPos(h: { y: number }, height: number): React.CSSProperties {
  return h.y > height - 90 ? { bottom: height - h.y + 14 } : { top: h.y + 14 }
}
