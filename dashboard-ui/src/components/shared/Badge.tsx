import type { ReactNode } from 'react'

export type BadgeVariant = 'success' | 'warning' | 'danger' | 'info' | 'neutral' | 'accent'
export type BadgeSize = 'xs' | 'sm' | 'md'

export interface BadgeProps {
  variant?: BadgeVariant
  size?: BadgeSize
  /** Optional leading icon/glyph (e.g. <ChevronIcon />, an emoji string, etc.) */
  icon?: ReactNode
  /** Show a colored dot before the label */
  dot?: boolean
  /** Native HTML title — shown as tooltip on hover */
  title?: string
  className?: string
  children: ReactNode
}

/**
 * Badge — unified semantic pill primitive.
 *
 * Replaces ad-hoc `bg-green-700/30 text-green-300 border-green-700/40` patterns
 * scattered across the codebase. All workers should migrate to this component.
 *
 * Usage:
 *   <Badge variant="success">Healthy</Badge>
 *   <Badge variant="warning" size="xs" dot>Borderline</Badge>
 *   <Badge variant="danger" icon="⚠">Halted</Badge>
 */

// Static class maps — all values must be statically analysable for Tailwind 4 purge
const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  success: 'bg-green-500/15 text-green-400 border-green-500/30',
  warning: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  danger:  'bg-red-500/15 text-red-400 border-red-500/30',
  info:    'bg-blue-500/15 text-blue-400 border-blue-500/30',
  neutral: 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)] border-[var(--color-border)]',
  accent:  'bg-indigo-500/15 text-indigo-400 border-indigo-500/30',
}

const DOT_CLASSES: Record<BadgeVariant, string> = {
  success: 'bg-green-400',
  warning: 'bg-amber-400',
  danger:  'bg-red-400',
  info:    'bg-blue-400',
  neutral: 'bg-[var(--color-text-muted)]',
  accent:  'bg-indigo-400',
}

const SIZE_CLASSES: Record<BadgeSize, string> = {
  xs: 'text-[9px] px-1.5 py-0.5',
  sm: 'text-[10px] px-2 py-0.5',
  md: 'text-[11px] px-2.5 py-1',
}

export function Badge({
  variant = 'neutral',
  size = 'sm',
  icon,
  dot = false,
  className = '',
  children,
  title,
}: BadgeProps) {
  return (
    <span
      title={title}
      className={[
        'inline-flex items-center gap-1 rounded-full border font-semibold font-mono tabular-nums leading-none',
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      ].join(' ')}
    >
      {dot && (
        <span
          className={`inline-block rounded-full flex-shrink-0 ${DOT_CLASSES[variant]}`}
          style={{ width: 5, height: 5 }}
          aria-hidden="true"
        />
      )}
      {icon && (
        <span className="flex-shrink-0 leading-none" aria-hidden="true">
          {icon}
        </span>
      )}
      {children}
    </span>
  )
}
