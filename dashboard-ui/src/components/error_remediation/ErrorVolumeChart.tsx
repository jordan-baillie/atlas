import { useEffect, useMemo, useState } from 'react'
import { Chart } from '../shared/Chart'
import type { ChartData, ChartOptions } from 'chart.js'

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

  const chartConfig = useMemo<ChartData<'line'>>(() => ({
    labels: data.map((d) => d.hour),
    datasets: [
      {
        label: 'Errors',
        data: data.map((d) => d.errors),
        borderColor: 'var(--color-red)',
        borderWidth: 2,
        fill: false,
        pointRadius: 0,
        pointHoverRadius: 3,
        tension: 0.25,
      },
    ],
  }), [data])

  const chartOptions = useMemo<ChartOptions<'line'>>(() => ({
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          title: (items) => (items[0]?.label ? fmtHourBucket(items[0].label) : ''),
          label: (ctx) => String(ctx.parsed.y),
        },
      },
    },
    scales: {
      x: {
        ticks: {
          color: 'var(--color-text-muted)',
          font: { size: 10 },
          maxRotation: 0,
          autoSkipPadding: 16,
          callback(value) {
            return fmtHourBucket(this.getLabelForValue(Number(value)) as string)
          },
        },
      },
      y: {
        beginAtZero: true,
        ticks: {
          color: 'var(--color-text-muted)',
          font: { size: 10 },
          precision: 0,
        },
      },
    },
    animation: { duration: 500, easing: 'easeOutQuart' },
  }), [])

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5">
      <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold mb-4">
        Error Volume — Last 24h (hourly)
      </div>

      {loading ? (
        <div className="h-[180px] bg-[var(--color-surface-alt)] rounded animate-pulse" />
      ) : error ? (
        <div className="h-[180px] flex items-center justify-center text-sm text-[var(--color-red)]">{error}</div>
      ) : data.length === 0 ? (
        <div className="h-[180px] flex items-center justify-center text-sm text-[var(--color-text-muted)]">
          No errors in the last 24h
        </div>
      ) : (
        <Chart
          kind="line"
          data={chartConfig as ChartData<'line' | 'bar' | 'doughnut'>}
          options={chartOptions as ChartOptions<'line' | 'bar' | 'doughnut'>}
          height={180}
        />
      )}
    </div>
  )
}
