import { memo } from 'react'
import type { MacroDimension } from '../../api/types'
import { Sparkline } from '../shared/Sparkline'

interface Props { dimension: MacroDimension }

function GaugeCardInner({ dimension }: Props) {
  const score = dimension.score ?? 0
  const weight = dimension.weight ?? 0
  const positive = score >= 0

  // Color coding based on score magnitude
  const fillColor = score > 0.5 ? '#22c55e' : score > 0 ? '#f59e0b' : score > -0.5 ? '#f97316' : '#ef4444'
  const bgTint = score > 0.5 ? 'rgba(34,197,94,0.06)' : score > 0 ? 'rgba(245,158,11,0.06)' : score > -0.5 ? 'rgba(249,115,22,0.06)' : 'rgba(239,68,68,0.06)'

  const widthPct = Math.min(Math.abs(score) * 50, 50)
  const scoreColor = positive ? 'text-[var(--color-green)]' : 'text-[var(--color-red)]'
  const signedScore = (score >= 0 ? '+' : '') + score.toFixed(3)

  // Signal icon based on score
  const signalIcon = score > 0.5 ? '▲' : score > 0 ? '△' : score > -0.5 ? '▽' : '▼'

  return (
    <div
      data-testid="macro-gauge"
      className="bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded-lg p-4 transition-colors hover:border-[color-mix(in_srgb,var(--color-border),var(--color-text)_20%)]"
      style={{ backgroundColor: bgTint }}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          <span style={{ color: fillColor }} className="text-xs">{signalIcon}</span>
          <div className="text-[10px] uppercase tracking-wider font-medium">{dimension.label ?? dimension.name ?? '\u2014'}</div>
        </div>
        <div className="text-[10px] text-[var(--color-text-muted)] font-mono bg-[var(--color-surface)] rounded px-1.5 py-0.5">
          {(weight * 100).toFixed(0)}%
        </div>
      </div>
      <div className="text-xs text-[var(--color-text-muted)] text-right font-mono mb-2">{dimension.raw_value ?? ''}</div>

      {/* Visual gauge bar */}
      <div className="h-1.5 bg-[var(--color-border)] rounded-full relative mb-2 overflow-hidden">
        <div className="absolute top-0 bottom-0 left-1/2 w-px bg-[var(--color-text-muted)]/30" />
        <div
          className="absolute top-0 bottom-0 rounded-full transition-all duration-700 ease-out"
          style={{
            left: positive ? '50%' : `${50 - widthPct}%`,
            width: `${widthPct}%`,
            backgroundColor: fillColor,
            boxShadow: `0 0 6px ${fillColor}40`,
          }}
        />
      </div>

      <div className={`font-mono text-sm font-semibold ${scoreColor}`}>{signedScore}</div>
      {dimension.sparkline && dimension.sparkline.length > 0 ? (
        <div className="mt-2">
          <Sparkline data={dimension.sparkline} height={24} />
        </div>
      ) : null}
    </div>
  )
}

export const GaugeCard = memo(GaugeCardInner)
