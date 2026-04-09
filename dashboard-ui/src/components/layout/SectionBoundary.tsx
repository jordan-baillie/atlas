import type { ReactNode } from 'react'
import { ErrorBoundary } from 'react-error-boundary'
import type { FallbackProps as RebFallbackProps } from 'react-error-boundary'
import { QueryErrorResetBoundary } from '@tanstack/react-query'

interface Props {
  children: ReactNode
  title: string
}

// Extends the library's FallbackProps to add the section title.
// error is typed as `unknown` (matching react-error-boundary v6) so we guard
// before accessing .message.
type ErrorFallbackProps = RebFallbackProps & { title: string }

// Module-scoped per rerender-no-inline-components rule — never re-created on
// parent render. Only rendered on the (rare) error path.
function ErrorFallback({ error, resetErrorBoundary, title }: ErrorFallbackProps) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-red)]/40 rounded-xl p-4">
      <div className="text-[11px] uppercase tracking-wider font-semibold text-[var(--color-red)] mb-1">
        {title} failed
      </div>
      <div className="text-sm text-[var(--color-text-muted)] mb-3">{message}</div>
      <button
        onClick={() => resetErrorBoundary()}
        className="text-xs px-3 py-1.5 bg-[var(--color-surface-alt)] hover:bg-[var(--color-border)] rounded-md transition-colors"
      >
        Retry
      </button>
    </div>
  )
}

// Note: `fallbackRender` below creates a new arrow function on every render of
// SectionBoundary. This is a known trade-off from react-error-boundary's API
// design — the callback must close over `title` to pass it to ErrorFallback.
// It is acceptable here because the error path is rare and the arrow function
// itself is trivially cheap. ErrorFallback is still module-scoped, satisfying
// the rerender-no-inline-components rule for the component definition itself.
export function SectionBoundary({ children, title }: Props) {
  return (
    <QueryErrorResetBoundary>
      {({ reset }) => (
        <ErrorBoundary
          onReset={reset}
          fallbackRender={(props) => <ErrorFallback {...props} title={title} />}
        >
          {children}
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  )
}
