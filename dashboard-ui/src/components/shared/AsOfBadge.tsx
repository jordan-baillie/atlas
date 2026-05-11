import { Badge } from './Badge'
import { humanRelative } from '../../utils/time'

interface AsOfBadgeProps {
  source: 'live' | 'snapshot'
  asOf?: string    // ISO timestamp; if omitted, shows just the source label
  title?: string   // tooltip text override
}

/**
 * AsOfBadge — small inline badge clarifying the data provenance of an equity value.
 *
 * "LIVE" (info/blue) = real-time broker pull, updates each request
 * "EOD" (neutral)    = end-of-day snapshot from market_equity_history, last NYSE close
 *
 * Designed to sit next to an equity number at 9px so it doesn't dominate.
 * Uses the Badge primitive — canonical first consumer of the new badge system.
 */
export function AsOfBadge({ source, asOf, title }: AsOfBadgeProps) {
  const sourceLabel = source === 'live' ? 'LIVE' : 'EOD'
  const variant = source === 'live' ? 'info' : 'neutral'

  const defaultTooltip =
    source === 'live'
      ? 'Live broker pull — updates each request'
      : 'End-of-day snapshot — last NYSE close'

  const tooltipText =
    title ?? (asOf ? `${defaultTooltip} (${new Date(asOf).toLocaleTimeString()})` : defaultTooltip)

  const relAge = asOf ? humanRelative(asOf) : ''

  return (
    <Badge variant={variant} size="xs" title={tooltipText} dot>
      {sourceLabel}
      {relAge && <> &middot; {relAge}</>}
    </Badge>
  )
}
