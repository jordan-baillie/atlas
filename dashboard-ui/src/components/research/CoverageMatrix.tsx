import { useResearchCoverage } from '../../api/research-queries'
import { Skeleton } from '../layout/Skeleton'
import { fmtNum } from '../../lib/format'
import type { CoverageCell, CoverageCellStatus } from '../../api/research-types'

const STATUS_COLOR: Record<CoverageCellStatus | 'never', string> = {
  fresh: 'bg-green-500/15 text-green-400 border-green-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  very_stale: 'bg-red-500/15 text-red-400 border-red-500/30',
  never: 'bg-zinc-500/10 text-zinc-500 border-zinc-700/50',
}

function Cell({ cell }: { cell: CoverageCell | null }) {
  if (!cell) {
    return (
      <td className={`px-2 py-2 text-center text-xs border ${STATUS_COLOR.never}`}>
        —
      </td>
    )
  }
  const cls = STATUS_COLOR[cell.status] ?? STATUS_COLOR.never
  return (
    <td
      className={`px-2 py-2 text-center text-xs border ${cls}`}
      title={cell.updated_at ?? ''}
    >
      <div className="font-mono">{fmtNum(cell.sharpe ?? 0, 2)}</div>
      <div className="text-[10px] opacity-75">
        {cell.age_days != null ? `${cell.age_days.toFixed(0)}d` : ''}
      </div>
    </td>
  )
}

export function CoverageMatrix({ enabled }: { enabled: boolean }) {
  const { data, isLoading, error } = useResearchCoverage(enabled)

  if (isLoading) return <Skeleton className="h-64" />
  if (error) return (
    <div className="text-red-400 text-sm p-4">Failed to load coverage matrix</div>
  )
  if (!data) return null

  const { strategies, universes, matrix } = data

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 className="text-sm font-semibold">Research Coverage Matrix</h3>
        <div className="flex items-center gap-3 text-[10px]">
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 rounded-sm bg-green-500/30 border border-green-500/40 inline-block" />
            Fresh (&lt;7d)
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 rounded-sm bg-amber-500/30 border border-amber-500/40 inline-block" />
            Stale (7-14d)
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 rounded-sm bg-red-500/30 border border-red-500/40 inline-block" />
            Very stale (≥14d)
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 rounded-sm bg-zinc-700 inline-block" />
            Never
          </span>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr>
              <th className="text-left px-2 py-2 sticky left-0 bg-[var(--color-surface)]">
                Strategy
              </th>
              {universes.map((u) => (
                <th key={u} className="px-2 py-2 text-center font-medium">
                  {u}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {strategies.map((s) => (
              <tr key={s}>
                <td className="text-left px-2 py-2 font-mono text-[11px] sticky left-0 bg-[var(--color-surface)]">
                  {s}
                </td>
                {universes.map((u) => (
                  <Cell key={u} cell={matrix[s]?.[u] ?? null} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
