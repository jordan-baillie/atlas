import { LineChart, Line, ResponsiveContainer, Tooltip } from 'recharts'

interface SparklineProps {
  data: number[]
  color?: string
  height?: number
  strokeWidth?: number
}

function SparklineTooltip({ active, payload }: { active?: boolean; payload?: Array<{ value?: number }> }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded px-2 py-1 text-[10px] font-mono shadow-md">
      {payload[0]?.value?.toLocaleString()}
    </div>
  )
}

export function Sparkline({ data, color, height = 32, strokeWidth = 1.5 }: SparklineProps) {
  if (!data || data.length === 0) {
    return <div style={{ height }} />
  }

  const resolvedColor =
    color ?? (data[data.length - 1] >= data[0] ? '#22c55e' : '#ef4444')

  const chartData = data.map((value, index) => ({ index, value }))

  return (
    <div style={{ height, width: '100%' }}>
      <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={0}>
        <LineChart data={chartData}>
          <defs>
            <filter id="glow">
              <feGaussianBlur stdDeviation="2" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>
          <Tooltip
            content={<SparklineTooltip />}
            cursor={false}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke={resolvedColor}
            strokeWidth={strokeWidth + 2}
            strokeOpacity={0.15}
            dot={false}
            isAnimationActive={false}
            activeDot={false}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke={resolvedColor}
            strokeWidth={strokeWidth}
            dot={false}
            isAnimationActive={true}
            animationDuration={800}
            animationEasing="ease-out"
            activeDot={{ r: 2, fill: resolvedColor }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
