/**
 * Chart.tsx -- Generic React wrapper around react-chartjs-2.
 *
 * Every chart in the dashboard renders through this component so theme
 * defaults (colours, fonts, grid, tooltip behaviour, animation policy)
 * apply uniformly.  Callers pass a flat config -- no JSX-children
 * pattern like Recharts.
 *
 * Usage:
 *   <Chart
 *     kind="line"
 *     data={{
 *       labels: ['Mon', 'Tue', ...],
 *       datasets: [{ label: 'Sharpe', data: [0.4, 0.5, 0.6] }],
 *     }}
 *     height={260}
 *   />
 *
 * Caller overrides via `options` are deep-merged onto the global
 * defaults from chart-defaults.ts.  See that file for what's tunable.
 */

import { useMemo, useRef, useEffect, useState } from 'react'
import { Chart as ReactChart } from 'react-chartjs-2'
import type {
  ChartData,
  ChartOptions,
  ChartType,
  ChartTypeRegistry,
  Plugin,
} from 'chart.js'

import { ensureChartRegistered } from '../../lib/chart-setup'
import {
  defaultChartOptions,
  drawInAnimation,
  mergeOptions,
  paletteFor,
  textBody,
  textMuted,
  seriesGrid,
} from '../../lib/chart-defaults'

ensureChartRegistered()

// Supported `kind` values + the Chart.js type they map to.
// 'area' is line+fill, 'sparkline' is line with everything off.
export type ChartKind = 'line' | 'area' | 'bar' | 'doughnut' | 'sparkline'

// After the kind->chart.js mapping below, every chart we render is one of
// these three underlying Chart.js types.  Typing options against this narrow
// union (rather than `keyof ChartTypeRegistry`) matches what callers actually
// build and what `defaultChartOptions()` returns -- avoids contravariant
// function-parameter mismatches on tooltip callbacks etc.
type SupportedChartType = 'line' | 'bar' | 'doughnut'

function chartJsType(kind: ChartKind): ChartType {
  if (kind === 'area' || kind === 'sparkline') return 'line'
  return kind
}

interface ChartProps<K extends ChartKind = ChartKind> {
  kind: K
  data: ChartData<keyof ChartTypeRegistry>
  options?: ChartOptions<SupportedChartType>
  /** Pixel height of the canvas wrapper.  Width is responsive. */
  height?: number | string
  /** Optional className for the wrapper div. */
  className?: string
  /** Chart.js plugins to register only for this chart instance. */
  plugins?: Plugin[]
  /** Strip axes/grid/legend for inline display.  Defaults true when kind='sparkline'. */
  bare?: boolean
  /** Progressive left->right line draw-in on FIRST render only (poll refetches
   *  morph instead of redrawing). No-op under prefers-reduced-motion. */
  drawIn?: boolean
}

/** Resolve a colour name or hex string to the actual stroke value. */
function resolveColor(c: unknown, fallback: string): string {
  if (typeof c !== 'string') return fallback
  if (c.startsWith('#') || c.startsWith('rgb') || c.startsWith('hsl') || c.startsWith('var(')) return c
  // Named tokens
  switch (c) {
    case 'green':    return '#22c55e'
    case 'red':      return '#ef4444'
    case 'amber':    return '#f59e0b'
    case 'blue':     return '#3b82f6'
    case 'indigo':   return '#6366f1'
    case 'teal':     return '#14b8a6'
    case 'pink':     return '#ec4899'
    case 'purple':   return '#a855f7'
    case 'muted':    return textMuted()
    case 'body':     return textBody()
    case 'grid':     return seriesGrid()
    default:         return fallback
  }
}

/** Apply palette defaults to a dataset when caller omitted colour fields. */
function normaliseDatasets(kind: ChartKind, data: ChartData<keyof ChartTypeRegistry>): ChartData<keyof ChartTypeRegistry> {
  const datasets = data.datasets.map((ds, i) => {
    const baseColor = resolveColor(
      (ds as { color?: unknown }).color ?? (ds as { borderColor?: unknown }).borderColor,
      paletteFor(i),
    )
    const isLineish = kind === 'line' || kind === 'area' || kind === 'sparkline'

    const merged: typeof ds = { ...ds }

    if (isLineish) {
      const lds = merged as typeof ds & {
        borderColor?: string
        backgroundColor?: string
        fill?: boolean | string | number
        tension?: number
        pointRadius?: number
        borderWidth?: number
      }
      lds.borderColor = lds.borderColor ?? baseColor
      // 'area' kind -> fill with a translucent version of the line colour
      if (kind === 'area' && lds.fill === undefined) {
        lds.fill = true
        lds.backgroundColor = lds.backgroundColor ?? `${baseColor}26` // ~15% opacity
      } else if (kind === 'sparkline') {
        lds.fill = false
        lds.pointRadius = lds.pointRadius ?? 0
        lds.borderWidth = lds.borderWidth ?? 1.5
      } else {
        lds.fill = lds.fill ?? false
      }
      lds.tension = lds.tension ?? 0.25
      return lds as typeof ds
    }

    if (kind === 'bar') {
      const bds = merged as typeof ds & { backgroundColor?: string; borderColor?: string }
      bds.backgroundColor = bds.backgroundColor ?? baseColor
      bds.borderColor = bds.borderColor ?? baseColor
      return bds as typeof ds
    }

    if (kind === 'doughnut') {
      const dds = merged as typeof ds & { backgroundColor?: string[] | string; borderColor?: string }
      const n = Array.isArray(ds.data) ? ds.data.length : 0
      dds.backgroundColor = dds.backgroundColor ?? Array.from({ length: n }, (_, j) => paletteFor(j))
      dds.borderColor = dds.borderColor ?? 'transparent'
      return dds as typeof ds
    }

    return merged
  })

  return { ...data, datasets } as ChartData<keyof ChartTypeRegistry>
}

/** Generate the per-kind extra options that get folded over the global defaults. */
function kindOverrides(kind: ChartKind): ChartOptions<SupportedChartType> {
  if (kind === 'sparkline') {
    return {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { enabled: true },
      },
      scales: {
        x: { display: false },
        y: { display: false },
      },
      elements: {
        point: { radius: 0, hoverRadius: 3 },
        line: { borderWidth: 1.5 },
      },
    } as ChartOptions<SupportedChartType>
  }

  if (kind === 'doughnut') {
    return {
      cutout: '62%',
      plugins: {
        legend: { display: true, position: 'right' },
      },
      scales: {},
    } as unknown as ChartOptions<SupportedChartType>
  }

  if (kind === 'bar') {
    return {
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true },
      },
    } as ChartOptions<SupportedChartType>
  }

  return {} as ChartOptions<SupportedChartType>
}

export function Chart<K extends ChartKind = ChartKind>({
  kind,
  data,
  options,
  height = 200,
  className,
  plugins,
  bare,
  drawIn = false,
}: ChartProps<K>) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  // drawIn applies only until the first paint completes; subsequent data
  // updates (polls) use the default morph animation.
  const [drawInDone, setDrawInDone] = useState(false)
  useEffect(() => {
    if (!drawIn || drawInDone) return
    const id = requestAnimationFrame(() => setDrawInDone(true))
    return () => cancelAnimationFrame(id)
  }, [drawIn, drawInDone])
  const isFirstRender = !drawInDone

  // Compose options once per render -- defaults < kind-specific < caller.
  const pointCount = Array.isArray(data.datasets?.[0]?.data) ? data.datasets[0].data.length : 0
  const mergedOptions = useMemo(() => {
    const base = defaultChartOptions()
    const k = kindOverrides(kind)
    const stage1 = mergeOptions(base, k)
    const stage2 = mergeOptions(stage1, options ?? {})
    if (bare && stage2.scales) {
      stage2.scales = {
        x: { display: false },
        y: { display: false },
      } as ChartOptions<SupportedChartType>['scales']
    }
    if (drawIn && isFirstRender) {
      const anim = drawInAnimation(pointCount)
      ;(stage2 as { animation?: unknown }).animation = anim
    }
    return stage2
  }, [kind, options, bare, drawIn, isFirstRender, pointCount])

  const normalisedData = useMemo(() => normaliseDatasets(kind, data), [kind, data])

  // Workaround for one Chart.js + react-chartjs-2 quirk: when the parent
  // resizes the canvas needs an explicit resize() call, otherwise the
  // chart fills its initial bounding box and never shrinks/grows.  The
  // ResizeObserver below catches container size changes.
  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      // react-chartjs-2 handles redraw on data/options change; this is
      // just to nudge for container-only changes.  No-op if no chart yet.
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return (
    <div
      ref={containerRef}
      className={className}
      style={{ height, width: '100%', position: 'relative' }}
    >
      <ReactChart
        type={chartJsType(kind)}
        data={normalisedData as ChartData<ChartType>}
        options={mergedOptions as ChartOptions<ChartType>}
        plugins={plugins}
      />
    </div>
  )
}
