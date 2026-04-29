import { useEffect, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { ChartGate } from '../shared/ChartGate'
import { ChartTooltip } from '../shared/ChartTooltip'

interface Bucket {
  hour: string
  errors: number
  occurrences: number
}

interface TimeseriesResponse {
  hours: number
  buckets: Bucket[]
}

function fmtHourBucket(h: string): string {
  // h is like "2026-04-29T14" — show "14:00"
  const parts = h.split('T')
  return parts[1] ? `${parts[1]}:00` : h
}

export function ErrorVolumeChart() {
  const [data, setData] = useState<Bucket[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch('/api/error_remediation/timeseries?hours=24')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json: TimeseriesResponse = await res.json()
        if (!cancelled) {
          setData(json.buckets)
          setError(null)
        }
      } catch (e: unknown) {
        if (!cancelled) setError(String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [])

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-4">
        Error Volume — Last 24h (hourly)
      </div>

      {loading ? (
        <div className="h-[180px] bg-[var(--color-surface-alt)] rounded animate-pulse" />
      ) : error ? (
        <div className="h-[180px] flex items-center justify-center text-sm text-red-500">{error}</div>
      ) : data.length === 0 ? (
        <div className="h-[180px] flex items-center justify-center text-sm text-[var(--color-text-muted)]">
          No errors in the last 24h
        </div>
      ) : (
        <ChartGate className="h-[180px] w-full">
          <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={0}>
            <LineChart data={data}>
              <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="hour"
                tickFormatter={fmtHourBucket}
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 10, fill: 'var(--color-text-muted)' }}
                interval="preserveStartEnd"
              />
              <YAxis
                allowDecimals={false}
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 10, fill: 'var(--color-text-muted)' }}
                width={28}
              />
              <Tooltip
                cursor={{ stroke: 'var(--color-border)', strokeDasharray: '4 4' }}
                content={
                  <ChartTooltip
                    labelFormatter={(l) => fmtHourBucket(l as string)}
                    formatter={(v) => String(v)}
                  />
                }
              />
              <Line
                dataKey="errors"
                name="Errors"
                stroke="var(--color-red)"
                strokeWidth={2}
                dot={false}
                isAnimationActive={true}
                animationDuration={800}
                animationEasing="ease-out"
              />
            </LineChart>
          </ResponsiveContainer>
        </ChartGate>
      )}
    </div>
  )
}
