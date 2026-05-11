import type { ReactNode } from 'react'

interface EmptyStateProps {
  /** Simple text mode — used by legacy callers. If `heading` is supplied, this is ignored. */
  message?: string
  /** Optional icon node (emoji, SVG, etc.). Rendered above heading. */
  icon?: ReactNode
  /** Bold heading line */
  heading?: string
  /** Muted description below heading */
  description?: string
  /** Optional call-to-action button */
  action?: { label: string; onClick: () => void }
  className?: string
}

export function EmptyState({
  message = 'No data available',
  icon,
  heading,
  description,
  action,
  className = '',
}: EmptyStateProps) {
  /* Rich layout when heading is provided */
  if (heading) {
    return (
      <div className={`flex flex-col items-center justify-center py-10 gap-3 text-center ${className}`}>
        {icon && (
          <div
            className="text-2xl text-[var(--color-text-muted)] select-none"
            aria-hidden="true"
          >
            {icon}
          </div>
        )}
        {/* Tightened from text-sm to text-[13px] per typography scale */}
        <p className="text-[13px] font-semibold text-[var(--color-text)]">{heading}</p>
        {description && (
          <p className="text-xs text-[var(--color-text-muted)] max-w-xs leading-relaxed">
            {description}
          </p>
        )}
        {action && (
          <button
            onClick={action.onClick}
            className="mt-1 inline-flex items-center px-3 py-1.5 rounded-md text-xs font-medium
                       bg-[var(--color-surface-alt)] border border-[var(--color-border)]
                       text-[var(--color-text-muted)] hover:text-[var(--color-text)]
                       transition-colors"
          >
            {action.label}
          </button>
        )}
      </div>
    )
  }

  /* Simple / legacy fallback */
  return (
    <div className={`text-center py-8 text-[13px] text-[var(--color-text-muted)] ${className}`}>
      {message}
    </div>
  )
}
