import { ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import type { PacePoint } from '../../api/types'
import { ChartTooltip } from '../shared/ChartTooltip'
import { fmtSignedCcy, fmtDateShort, fmtCcy } from '../../lib/format'

interface Props {
  paceData: PacePoint[]
  paceStatus?: string
  paceDiff?: number
}

function badgeClass(status: string | undefined): string {
  if (status === 'under') return 'bg-[var(--color-green)]/20 text-[var(--color-green)]'
  if (status === 'over') return 'bg-[var(--color-red)]/20 text-[var(--color-red)]'
  return 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]'
}

export function SpendingPaceChart({ paceData, paceStatus, paceDiff }: Props) {
  // Derive budget target from last budget data point
  const budgetTarget = paceData.length > 0
    ? paceData[paceData.length - 1]?.budget
    : undefined

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      <div className="flex items-center justify-between mb-4">
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">SPENDING PACE</div>
        {paceStatus != null ? (
          <div className={`rounded-full px-3 py-1 text-[10px] font-mono font-medium uppercase ${badgeClass(paceStatus)}`}>
            {paceStatus} {paceDiff != null ? fmtSignedCcy(paceDiff) : null}
          </div>
        ) : null}
      </div>
      <ResponsiveContainer width="100%" height={280} minWidth={0} minHeight={0}>
        <ComposedChart data={paceData}>
          <defs>
            <linearGradient id="spendingGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#22c55e" stopOpacity={0.2} />
              <stop offset="100%" stopColor="#22c55e" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={(v) => fmtDateShort(v as string)}
            axisLine={false}
            tickLine={false}
            tick={{ fontSize: 10, fill: 'var(--color-text-muted)' }}
          />
          <YAxis
            tickFormatter={(v) => '$' + Math.round(v as number).toLocaleString('en-US')}
            axisLine={false}
            tickLine={false}
            tick={{ fontSize: 10, fill: 'var(--color-text-muted)' }}
          />
          <Tooltip
            cursor={{ stroke: 'var(--color-border)', strokeDasharray: '4 4' }}
            content={
              <ChartTooltip
                labelFormatter={(l) => fmtDateShort(l)}
                formatter={(v) => fmtCcy(v)}
              />
            }
          />
          {budgetTarget != null ? (
            <ReferenceLine
              y={budgetTarget}
              stroke="var(--color-text-muted)"
              strokeDasharray="6 4"
              strokeOpacity={0.5}
            />
          ) : null}
          <Area
            dataKey="actual"
            name="Actual"
            stroke="#22c55e"
            strokeWidth={2}
            fill="url(#spendingGrad)"
            baseValue={0}
            dot={false}
            isAnimationActive={true}
            animationDuration={1200}
            animationEasing="ease-out"
          />
          <Line
            dataKey="budget"
            name="Budget"
            stroke="#a1a1aa"
            strokeDasharray="4 4"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={true}
            animationDuration={1200}
            animationEasing="ease-out"
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
