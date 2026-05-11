interface StatusDotProps {
  status: 'green' | 'amber' | 'red' | 'gray'
  /** Dot diameter: sm=6px (default), md=8px, lg=10px */
  size?: 'sm' | 'md' | 'lg'
  /** When true, applies a slow opacity pulse — use for live/real-time indicators */
  pulse?: boolean
  className?: string
}

const STATUS_COLORS: Record<StatusDotProps['status'], string> = {
  green: '#22c55e',
  amber: '#f59e0b',
  red:   '#ef4444',
  gray:  '#a1a1aa',
}

const SIZE_PX: Record<NonNullable<StatusDotProps['size']>, number> = {
  sm: 6,
  md: 8,
  lg: 10,
}

/**
 * StatusDot — colored inline dot for status indicators.
 *
 * Sizes: sm=6px (default), md=8px, lg=10px.
 * Pulse: slow opacity animation for live/real-time indicators.
 *        Respects prefers-reduced-motion via .status-pulse CSS class guard in index.css.
 */
export function StatusDot({ status, size = 'sm', pulse = false, className = '' }: StatusDotProps) {
  const px = SIZE_PX[size]
  return (
    <span
      className={`inline-block rounded-full flex-shrink-0 ${pulse ? 'status-pulse' : ''} ${className}`}
      style={{
        width: px,
        height: px,
        backgroundColor: STATUS_COLORS[status],
        animation: pulse ? 'status-pulse 1.8s ease-in-out infinite' : undefined,
      }}
      aria-hidden="true"
    />
  )
}
