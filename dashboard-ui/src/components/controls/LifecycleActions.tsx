/**
 * LifecycleActions — pure presentational component.
 * Renders 1–2 action buttons based on the current lifecycle state.
 * Calls onAction with the action type; parent owns modal state.
 *
 * Button palette:
 *   ACCENT  — primary promotions (promote_paper, promote_live, revive)
 *   WARN    — soft rollbacks (rollback, rollback_paper)
 *   DANGER  — destructive (retire)
 */

import type { LifecycleRow, LifecycleActionType } from '../../api/lifecycle'

interface Props {
  row: LifecycleRow
  onAction: (action: LifecycleActionType) => void
  disabled?: boolean
}

// ── Button class variants ─────────────────────────────────────────────────

/** Primary / promotion — accent border + tint */
const ACCENT =
  'h-8 px-2.5 rounded-md text-xs font-medium border ' +
  'border-[var(--color-accent)] text-[var(--color-accent)] ' +
  'bg-[var(--color-accent)]/5 hover:bg-[var(--color-accent)]/15 ' +
  'disabled:opacity-50 disabled:cursor-not-allowed transition-colors'

/** Soft rollback — amber border + tint */
const WARN =
  'h-8 px-2.5 rounded-md text-xs font-medium border ' +
  'border-amber-500/50 text-amber-400 ' +
  'bg-amber-500/5 hover:bg-amber-500/15 ' +
  'disabled:opacity-50 disabled:cursor-not-allowed transition-colors'

/** Destructive — danger border + tint */
const DANGER =
  'h-8 px-2.5 rounded-md text-xs font-medium border ' +
  'border-red-500/50 text-red-400 ' +
  'bg-red-500/5 hover:bg-red-500/15 ' +
  'disabled:opacity-50 disabled:cursor-not-allowed transition-colors'

// ── Component ─────────────────────────────────────────────────────────────

export function LifecycleActions({ row, onAction, disabled = false }: Props) {
  switch (row.state) {
    case 'RESEARCH':
      return (
        <button
          disabled={disabled}
          onClick={() => onAction('promote_paper')}
          className={ACCENT}
          data-testid="action-promote-paper"
        >
          Promote to PAPER ↑
        </button>
      )

    case 'PAPER':
      return (
        <span className="inline-flex items-center gap-1.5 flex-wrap">
          <button
            disabled={disabled}
            onClick={() => onAction('promote_live')}
            className={ACCENT}
            data-testid="action-promote-live"
          >
            Promote to LIVE ↑
          </button>
          <button
            disabled={disabled}
            onClick={() => onAction('rollback')}
            className={WARN}
            data-testid="action-rollback"
          >
            ↩ Rollback
          </button>
        </span>
      )

    case 'LIVE':
      return (
        <span className="inline-flex items-center gap-1.5 flex-wrap">
          <button
            disabled={disabled}
            onClick={() => onAction('rollback_paper')}
            className={WARN}
            data-testid="action-rollback-paper"
          >
            ↩ Soft rollback
          </button>
          <button
            disabled={disabled}
            onClick={() => onAction('retire')}
            className={DANGER}
            data-testid="action-retire"
          >
            ⏹ Retire
          </button>
        </span>
      )

    case 'RETIRED':
      return (
        <button
          disabled={disabled}
          onClick={() => onAction('revive')}
          className={ACCENT}
          data-testid="action-revive"
        >
          ↑ Revive
        </button>
      )

    default:
      return null
  }
}
