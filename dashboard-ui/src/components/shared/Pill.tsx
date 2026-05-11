import type { ReactNode } from 'react'
import { Badge } from './Badge'
import type { BadgeVariant } from './Badge'

export type PillStatus = 'live' | 'eod' | 'stale' | 'active' | 'inactive' | 'pending'

interface PillStatusConfig {
  variant: BadgeVariant
  label: string
}

const STATUS_CONFIG: Record<PillStatus, PillStatusConfig> = {
  live:     { variant: 'success', label: 'Live' },
  eod:      { variant: 'neutral', label: 'EOD' },
  stale:    { variant: 'danger',  label: 'Stale' },
  active:   { variant: 'info',    label: 'Active' },
  inactive: { variant: 'neutral', label: 'Inactive' },
  pending:  { variant: 'warning', label: 'Pending' },
}

interface PillProps {
  /** Named status shorthand — resolves to the right variant + default label */
  status?: PillStatus
  /** Override or custom Badge variant when not using a named status */
  variant?: BadgeVariant
  /** Override label; falls back to STATUS_CONFIG label when status is set */
  children?: ReactNode
  className?: string
}

/**
 * Pill — compact dot + label for inline status indicators.
 *
 * Named statuses: live, eod, stale, active, inactive, pending.
 * Wraps Badge with dot=true, size="xs" defaults.
 *
 * Usage:
 *   <Pill status="live" />           → green dot + "Live"
 *   <Pill status="eod" />            → neutral dot + "EOD"
 *   <Pill variant="warning">Sync</Pill>   → amber dot + "Sync"
 */
export function Pill({ status, variant, children, className = '' }: PillProps) {
  const cfg = status ? STATUS_CONFIG[status] : null
  const resolvedVariant = variant ?? cfg?.variant ?? 'neutral'
  const resolvedLabel = children ?? cfg?.label ?? ''

  return (
    <Badge variant={resolvedVariant} size="xs" dot className={className}>
      {resolvedLabel}
    </Badge>
  )
}
