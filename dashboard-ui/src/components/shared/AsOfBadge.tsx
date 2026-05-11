import { humanRelative } from '../../utils/time'

interface AsOfBadgeProps {
  source: 'live' | 'snapshot'
  asOf?: string    // ISO timestamp; if omitted, shows just the source label
  title?: string   // tooltip text override
}

/**
 * AsOfBadge -- small inline badge clarifying the data provenance of an equity value.
 *
 * "LIVE" (blue)     = real-time broker pull, updates each request
 * "EOD" (slate)     = end-of-day snapshot from market_equity_history, last NYSE close
 *
 * Designed to sit next to an equity number at text-[9px] so it doesn't dominate.
 */
export function AsOfBadge({ source, asOf, title }: AsOfBadgeProps) {
  const sourceLabel = source === 'live' ? 'LIVE' : 'EOD'

  const colorClass =
    source === 'live'
      ? 'bg-blue-500/15 text-blue-300 border-blue-500/30'
      : 'bg-slate-500/15 text-slate-300 border-slate-500/30'

  const defaultTooltip =
    source === 'live'
      ? 'Live broker pull — updates each request'
      : 'End-of-day snapshot — last NYSE close'

  const tooltipText = title ?? (asOf ? `${defaultTooltip} (${new Date(asOf).toLocaleTimeString()})` : defaultTooltip)

  const relAge = asOf ? humanRelative(asOf) : ''

  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[9px] font-mono uppercase border ${colorClass}`}
      title={tooltipText}
    >
      {sourceLabel}
      {relAge && <> &middot; {relAge}</>}
    </span>
  )
}
