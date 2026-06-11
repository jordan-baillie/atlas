import { useCountUp } from '../../hooks/useCountUp'
import { useDeltaFlash } from '../../hooks/useDeltaFlash'

interface AnimatedNumberProps {
  value: number | null | undefined
  format?: (v: number) => string
  className?: string
  /** Pulse the background green/red when the value moves. */
  flashOnDelta?: boolean
  duration?: number
  fallback?: string
}

/** Count-up numeral with optional delta flash. Mono + tabular by default. */
export function AnimatedNumber({
  value,
  format = (v) => String(Math.round(v)),
  className = '',
  flashOnDelta = false,
  duration,
  fallback = '—',
}: AnimatedNumberProps) {
  const display = useCountUp(value, { duration })
  const { direction, flashId } = useDeltaFlash(flashOnDelta ? value : null)

  const flashClass = direction === 'up' ? 'mc-flash-up' : direction === 'down' ? 'mc-flash-down' : ''
  return (
    <span
      key={flashOnDelta ? flashId : undefined}
      className={`font-mono tabular rounded px-0.5 -mx-0.5 ${flashClass} ${className}`}
    >
      {display == null ? fallback : format(display)}
    </span>
  )
}
