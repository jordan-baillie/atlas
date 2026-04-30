import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  fallback?: ReactNode
}
interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ErrorBoundary] Uncaught error:', error, info.componentStack)
  }

  reset = () => this.setState({ error: null })

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback

      return (
        <div className="p-5 rounded-xl border border-[var(--color-red)]/30 bg-[var(--color-red)]/8 flex flex-col gap-3">
          {/* Icon + title row */}
          <div className="flex items-center gap-2.5">
            <span className="text-[var(--color-red)] text-lg leading-none select-none" aria-hidden="true">
              &#9888;
            </span>
            <span className="font-mono font-semibold text-sm text-[var(--color-red)] tracking-tight">
              Something went wrong
            </span>
          </div>

          {/* Error message */}
          <p className="font-mono text-xs text-[var(--color-text-muted)] leading-relaxed pl-7">
            {this.state.error.message || 'An unexpected error occurred in this component.'}
          </p>

          {/* Reset button */}
          <div className="pl-7">
            <button
              onClick={this.reset}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium font-mono
                         bg-[var(--color-surface-alt)] border border-[var(--color-border)]
                         text-[var(--color-text-muted)] hover:text-[var(--color-text)]
                         hover:border-[var(--color-red)]/40 transition-colors"
            >
              &#8635; Try again
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
