/**
 * ChangeStateModal — multi-gate confirmation modal for universe and strategy state changes.
 *
 * Used by UniverseRow (universe scope) and StrategyRow (strategy scope).
 * Implements 6 submit gates: state change, reason length, i_understand checkbox,
 * type-to-confirm (universe + production only), open-positions blocker
 * (universe + disabled target only), and not-busy guard.
 *
 * Spec: §8.5 (universe) and §8.6 (strategy).
 */

import { useState, useEffect, useMemo, useRef } from 'react'
import { fmtSignedCcy, fmtNum } from '../../lib/format'
import { transition as lifecycleTransition, promotePaper } from '../../api/lifecycle'
import { ApiError } from '../../api/client'
import type { LifecycleActionType, LifecycleRow, LifecycleState, PromotionResponse } from '../../api/lifecycle'

// ─── Public types ─────────────────────────────────────────────

export type ChangeStateModalScope = 'universe' | 'strategy'

export interface ChangeStateModalProps {
  open: boolean
  onClose: () => void
  scope: ChangeStateModalScope
  marketId: string
  /** Strategy scope only */
  strategyName?: string
  /** Current effective state: 'live'|'passive'|'disabled' (universe) or 'enabled'|'disabled' (strategy) */
  currentState: string
  /** Source of current state: config JSON or an active override */
  currentSource: 'config' | 'override'
  /**
   * True when current universe state is 'live' (production guard).
   * Universe scope: type-to-confirm required.
   * Strategy scope: type-to-confirm NOT required per §7.2.
   */
  isProduction: boolean
  /** Pre-fill target state (e.g. opened from a Revert flow) */
  initialTargetState?: string
  /** Pre-fill reason (e.g. opened from a Revert flow) */
  initialReason?: string
  /** Universe scope: number of open positions (drives disabled blocker) */
  openPositions?: number
  /** Strategy scope display */
  trades30d?: number
  pnl30d?: number
  lifecycle?: string
  /**
   * Caller-provided submit function. Resolves on 2xx, throws on error.
   * expires_at: undefined = backend default 30d, null = never, ISO string = explicit.
   */
  onSubmit: (req: {
    state: string
    reason: string
    expires_at: string | null | undefined
    confirm_token?: string
    i_understand: boolean
  }) => Promise<void>
}

// ─── Internal types ───────────────────────────────────────────

type ExpiryChoice = '30d' | '7d' | '24h' | 'never'

interface StateOption {
  state: string
  emoji: string
  label: string
  desc: string
}

// ─── Module-level helpers (not inline inside the component) ───────────────

const UNIVERSE_OPTIONS: StateOption[] = [
  { state: 'live',     emoji: '🟢',  label: 'LIVE',     desc: 'Normal trading — entries, monitoring, OCO, EOD active' },
  { state: 'passive',  emoji: '🟡', label: 'PASSIVE',  desc: 'No new entries; maintain existing positions' },
  { state: 'disabled', emoji: '⚫',  label: 'DISABLED', desc: 'Full kill — no new monitoring, no maintenance' },
]

const STRATEGY_OPTIONS: StateOption[] = [
  { state: 'enabled',  emoji: '✓',   label: 'ENABLED',  desc: 'Strategy generates signals normally' },
  { state: 'disabled', emoji: '—', label: 'DISABLED', desc: 'No new signals; existing positions unaffected' },
]

function computeDefaultState(
  scope: ChangeStateModalScope,
  currentState: string,
  initialTargetState: string | undefined,
): string {
  if (initialTargetState && initialTargetState !== currentState) return initialTargetState
  const options = scope === 'universe' ? UNIVERSE_OPTIONS : STRATEGY_OPTIONS
  return options.find(o => o.state !== currentState)?.state ?? options[0].state
}

function expiryToApiValue(choice: ExpiryChoice): string | null | undefined {
  if (choice === '30d') return undefined  // backend defaults to 30d
  if (choice === 'never') return null     // explicit permanent (no auto-expiry)
  const d = new Date()
  if (choice === '7d') d.setDate(d.getDate() + 7)
  if (choice === '24h') d.setHours(d.getHours() + 24)
  return d.toISOString()
}

function stateBadgeClasses(state: string): string {
  switch (state) {
    case 'live':     return 'bg-green-500/15 text-green-400 border border-green-500/30'
    case 'passive':  return 'bg-yellow-500/15 text-yellow-400 border border-yellow-500/30'
    case 'disabled': return 'bg-zinc-500/15 text-zinc-400 border border-zinc-500/30'
    case 'enabled':  return 'text-green-400'
    default:         return 'text-zinc-400'
  }
}

function submitBtnClasses(targetState: string): string {
  if (targetState === 'disabled') {
    return 'bg-red-500/15 text-red-400 border border-red-500/30 hover:bg-red-500/25'
  }
  if (targetState === 'live' || targetState === 'enabled') {
    return 'bg-green-500/15 text-green-400 border border-green-500/30 hover:bg-green-500/25'
  }
  return 'bg-amber-500/15 text-amber-400 border border-amber-500/30 hover:bg-amber-500/25'
}

function confirmInputBorderClass(text: string, marketId: string): string {
  if (text === '') return 'border-zinc-500'
  return text === marketId ? 'border-green-500' : 'border-red-500'
}

function charCountClass(len: number): string {
  if (len < 10) return 'text-red-400'
  if (len > 500) return 'text-amber-400'
  return 'text-[var(--color-text-muted)]'
}

/**
 * Extract a display-friendly message from a thrown value.
 * Works whether Worker A's ApiError class has been merged or not:
 * - if the thrown object has a `detail` property (FastAPI error shape), use it
 * - otherwise fall back to Error.message, or String(e)
 */
function extractErrorMessage(e: unknown): string {
  if (e != null && typeof e === 'object' && 'detail' in e) {
    return String((e as { detail: unknown }).detail)
  }
  if (e instanceof Error) return e.message
  return String(e)
}

// ─── Component ─────────────────────────────────────────────────────

export function ChangeStateModal(props: ChangeStateModalProps) {
  const {
    open,
    onClose,
    scope,
    marketId,
    strategyName,
    currentState,
    currentSource,
    isProduction,
    initialTargetState,
    initialReason,
    openPositions,
    trades30d,
    pnl30d,
    lifecycle,
    onSubmit,
  } = props

  // ── Local state ───────────────────────────────────────────────

  const [selectedState, setSelectedState] = useState<string>(() =>
    computeDefaultState(scope, currentState, initialTargetState),
  )
  const [reason, setReason]             = useState<string>(initialReason ?? '')
  const [expiryChoice, setExpiryChoice] = useState<ExpiryChoice>('30d')
  const [iUnderstand, setIUnderstand]   = useState(false)
  const [confirmText, setConfirmText]   = useState('')
  const [busy, setBusy]                 = useState(false)
  const [error, setError]               = useState<string | null>(null)
  const [success, setSuccess]           = useState(false)

  const reasonRef = useRef<HTMLTextAreaElement>(null)

  // ── Reset all state whenever the modal opens ───────────────────────────────

  useEffect(() => {
    if (!open) return
    setSelectedState(computeDefaultState(scope, currentState, initialTargetState))
    setReason(initialReason ?? '')
    setExpiryChoice('30d')
    setIUnderstand(false)
    setConfirmText('')
    setBusy(false)
    setError(null)
    setSuccess(false)
    // Auto-focus the reason textarea (basic focus management on open)
    const t = setTimeout(() => reasonRef.current?.focus(), 50)
    return () => clearTimeout(t)
  }, [open, scope, currentState, initialTargetState, initialReason])

  // ── ESC closes (unless busy) ────────────────────────────────────────────

  useEffect(() => {
    if (!open) return
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !busy) onClose()
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [open, busy, onClose])

  // ── Derived booleans ─────────────────────────────────────────────────

  /** Universe + disabled selected + positions open → block submit */
  const showDisabledBlocker =
    scope === 'universe' && selectedState === 'disabled' && (openPositions ?? 0) > 0

  /** Universe + isProduction → show type-to-confirm field */
  const requireTypeConfirm = scope === 'universe' && isProduction

  // ── Submit gate — useMemo keeps disabled prop stable across re-renders ───────────
  //
  // Gate 1: selectedState !== currentState           (no-op guard)
  // Gate 2: reason >= 10 chars AND <= 500 chars      (required field + length limits)
  // Gate 3: iUnderstand checkbox ticked              (explicit operator acknowledgement)
  // Gate 4: universe+production → confirmText === marketId   (type-to-confirm)
  // Gate 5: universe+disabled → openPositions === 0     (open-positions blocker)
  // Gate 6: not currently submitting                 (busy guard)

  const canSubmit = useMemo<boolean>(() => {
    if (selectedState === currentState) return false
    if (reason.length < 10 || reason.length > 500) return false
    if (!iUnderstand) return false
    if (requireTypeConfirm && confirmText !== marketId) return false
    if (showDisabledBlocker) return false
    if (busy) return false
    return true
  }, [
    selectedState, currentState,
    reason,
    iUnderstand,
    requireTypeConfirm, confirmText, marketId,
    showDisabledBlocker,
    busy,
  ])

  // ── Derived display values ────────────────────────────────────────────

  const stateOptions = scope === 'universe' ? UNIVERSE_OPTIONS : STRATEGY_OPTIONS

  const submitLabel =
    scope === 'universe'
      ? 'Apply change'
      : selectedState === 'disabled' ? 'Disable' : 'Enable'

  const title =
    scope === 'universe'
      ? `Change state for ${marketId}`
      : `Toggle ${marketId}.${strategyName ?? ''}`

  // ── Submit handler ────────────────────────────────────────────────

  async function handleSubmit() {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await onSubmit({
        state: selectedState,
        reason,
        expires_at: expiryToApiValue(expiryChoice),
        // confirm_token: the typed market ID for production universes; undefined for all else
        confirm_token: requireTypeConfirm ? confirmText : undefined,
        i_understand: true,
      })
      setSuccess(true)
      setTimeout(() => onClose(), 2000)
    } catch (e: unknown) {
      setError(extractErrorMessage(e))
      setBusy(false)
    }
  }

  // ── Early-exit when closed ─────────────────────────────────────────────

  if (!open) return null

  // ── Success state — shown for 2 seconds before auto-close ──────────────────

  if (success) {
    const successLabel = `${marketId}${strategyName ? '.' + strategyName : ''} → ${selectedState.toUpperCase()}`
    return (
      <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-8 shadow-2xl flex flex-col items-center gap-4 max-w-md w-full">
          <div className="text-5xl text-green-400">✓</div>
          <div className="text-base font-mono text-green-400 text-center">{successLabel}</div>
        </div>
      </div>
    )
  }

  // ── Normal modal ───────────────────────────────────────────────────────

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={e => {
        // Backdrop click: close unless busy. No dirty-close prompt per spec §8.5.
        if (!busy && e.target === e.currentTarget) onClose()
      }}
    >
      {/* Modal box — stop propagation so backdrop handler does not fire on inner clicks */}
      <div
        className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 shadow-2xl max-w-md md:max-w-lg w-full"
        onClick={e => e.stopPropagation()}
      >

        {/* ── Header ─────────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-2 mb-4">
          <div className="min-w-0">
            <h2 className="text-base font-semibold leading-snug">{title}</h2>

            {/* Current-state subline */}
            <div className="mt-1 flex items-center gap-1.5 flex-wrap text-xs text-[var(--color-text-muted)]">
              <span>Current:</span>
              <span className={`px-2 py-0.5 rounded font-mono ${stateBadgeClasses(currentState)}`}>
                {currentState.toUpperCase()}
              </span>
              <span>(source: {currentSource})</span>
            </div>

            {/* Strategy info row (§8.6) */}
            {scope === 'strategy' && (
              <div className="mt-1 text-xs text-[var(--color-text-muted)]">
                Recent (30d):{' '}
                <span className="font-mono text-[var(--color-text)]">{trades30d ?? 0}</span>{' '}
                trades,{' '}
                <span className="font-mono">{fmtSignedCcy(pnl30d ?? 0)}</span>{' '}
                PnL, lifecycle{' '}
                <span className="font-mono text-[var(--color-text)]">{lifecycle ?? 'UNKNOWN'}</span>
              </div>
            )}
          </div>

          {/* Close × button */}
          <button
            type="button"
            onClick={() => { if (!busy) onClose() }}
            className="flex-shrink-0 text-[var(--color-text-muted)] hover:text-[var(--color-text)] text-xl leading-none mt-0.5"
            aria-label="Close modal"
          >
            ×
          </button>
        </div>

        {/* ── Form steps ─────────────────────────────────────────────── */}
        <div className="space-y-4">

          {/* Step 1 — State picker */}
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">
              New state
            </div>
            <div className="space-y-1">
              {stateOptions.map(opt => {
                const isCurrent = opt.state === currentState
                return (
                  <label
                    key={opt.state}
                    className={[
                      'flex items-start gap-3 rounded p-2 border border-transparent',
                      'transition-colors select-none',
                      isCurrent
                        ? 'opacity-50 cursor-not-allowed'
                        : 'cursor-pointer hover:border-[var(--color-border)]',
                    ].join(' ')}
                  >
                    <input
                      type="radio"
                      name="modal-state"
                      value={opt.state}
                      checked={selectedState === opt.state}
                      disabled={isCurrent}
                      onChange={() => { if (!isCurrent) setSelectedState(opt.state) }}
                      className="mt-0.5 flex-shrink-0"
                    />
                    <span className="text-sm">
                      <span className="font-medium">{opt.emoji} {opt.label}</span>
                      <span className="ml-2 text-xs text-[var(--color-text-muted)]">{opt.desc}</span>
                    </span>
                  </label>
                )
              })}
            </div>
          </div>

          {/* Step 2 — Reason textarea */}
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">
              Reason (required, ≥10 chars)
            </label>
            <textarea
              ref={reasonRef}
              rows={3}
              value={reason}
              onChange={e => setReason(e.target.value)}
              onKeyDown={e => {
                // Enter → newline (default textarea behaviour); stop propagation only
                if (e.key === 'Enter') e.stopPropagation()
              }}
              placeholder="Describe why you are making this change…"
              className="w-full bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded p-2 text-sm font-mono resize-none focus:outline-none focus:ring-1 focus:ring-[var(--color-border)]"
            />
            <div className={`text-xs mt-0.5 text-right tabular-nums ${charCountClass(reason.length)}`}>
              {reason.length} / 500
            </div>
          </div>

          {/* Step 3 — Auto-expire radio group */}
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">
              Auto-expire after
            </div>
            <div className="flex flex-wrap gap-x-5 gap-y-2">
              {(
                [
                  ['30d',   '30 days'],
                  ['7d',    '7 days'],
                  ['24h',   '24 hours'],
                  ['never', 'Never (warns)'],
                ] as [ExpiryChoice, string][]
              ).map(([val, lbl]) => (
                <label key={val} className="flex items-center gap-1.5 cursor-pointer select-none">
                  <input
                    type="radio"
                    name="modal-expiry"
                    value={val}
                    checked={expiryChoice === val}
                    onChange={() => setExpiryChoice(val)}
                  />
                  <span className={`text-sm ${val === 'never' ? 'text-amber-400' : ''}`}>
                    {lbl}
                  </span>
                </label>
              ))}
            </div>
            {expiryChoice === 'never' && (
              <div className="mt-2 text-xs text-amber-400 bg-amber-500/10 border border-amber-500/30 rounded p-2">
                ⚠ Override will not auto-expire. Operator must manually revert.
              </div>
            )}
          </div>

          {/* Step 4 — Open-positions blocker (universe + disabled + open positions only) */}
          {showDisabledBlocker && (
            <div className="bg-red-500/10 border border-red-500/30 rounded p-3 text-sm text-red-400">
              ⚠ Cannot disable while {openPositions} position(s) are open. Set PASSIVE first,
              close positions, then return to disable.
            </div>
          )}

          {/* Step 5 — "I understand" checkbox */}
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={iUnderstand}
              onChange={e => setIUnderstand(e.target.checked)}
            />
            <span className="text-sm">I understand this affects live trading.</span>
          </label>

          {/* Step 6 — Type-to-confirm (universe scope + isProduction only; NOT for strategy per §7.2) */}
          {requireTypeConfirm && (
            <div>
              <label className="block text-xs text-[var(--color-text-muted)] mb-1">
                Type the universe name (
                <span className="font-mono text-[var(--color-text)]">{marketId}</span>
                ) to confirm:
              </label>
              <input
                type="text"
                value={confirmText}
                onChange={e => setConfirmText(e.target.value)}
                onKeyDown={e => {
                  // Prevent Enter bubbling — avoids any accidental submission path
                  if (e.key === 'Enter') e.preventDefault()
                }}
                placeholder={marketId}
                spellCheck={false}
                autoComplete="off"
                className={[
                  'w-full bg-[var(--color-surface-alt)] rounded p-2 text-sm font-mono',
                  'border focus:outline-none transition-colors',
                  confirmInputBorderClass(confirmText, marketId),
                ].join(' ')}
              />
            </div>
          )}

          {/* Error banner — below steps, above action row */}
          {error !== null && (
            <div className="bg-red-500/10 border border-red-500/30 rounded p-2 text-xs text-red-400">
              {error}
            </div>
          )}

          {/* Action row */}
          <div className="flex items-center justify-end gap-3 pt-2 border-t border-[var(--color-border)]">
            <button
              type="button"
              disabled={busy}
              onClick={() => { if (!busy) onClose() }}
              className="text-[var(--color-text-muted)] hover:text-[var(--color-text)] px-4 py-2 text-sm disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!canSubmit}
              onClick={() => { void handleSubmit() }}
              className={[
                'px-4 py-2 rounded text-sm font-medium',
                'disabled:opacity-40 disabled:cursor-not-allowed',
                submitBtnClasses(selectedState),
              ].join(' ')}
            >
              {busy ? (
                <span className="flex items-center gap-1.5">
                  <span className="inline-block animate-spin">⟳</span>
                  Submitting…
                </span>
              ) : (
                submitLabel
              )}
            </button>
          </div>

        </div>{/* end form steps */}
      </div>{/* end modal box */}
    </div>
  )
}


// ═══════════════════════════════════════════════════════════════════
// LifecycleTransitionModal — lifecycle-specific transition confirmation
// ═══════════════════════════════════════════════════════════════════

// ── Action configuration ────────────────────────────────────────────

interface ActionConfig {
  label: string
  desc: string
  /** Show "I understand" checkbox */
  requiresUnderstand: boolean
  /** Uses POST /promote-paper instead of POST /transition */
  usesPromotePaper: boolean
  /** Target lifecycle state */
  targetState: LifecycleState
}

const ACTION_CFG: Record<LifecycleActionType, ActionConfig> = {
  promote_paper: {
    label: 'Promote to PAPER',
    desc:  'Begin paper trading. The strategy will simulate trades without real capital.',
    requiresUnderstand: false,
    usesPromotePaper:   false,
    targetState:        'PAPER',
  },
  promote_live: {
    label: 'Promote to LIVE',
    desc:  'Gate checks will run automatically. If all pass, the strategy goes live.',
    requiresUnderstand: false,
    usesPromotePaper:   true,
    targetState:        'LIVE',
  },
  rollback: {
    label: 'Rollback to RESEARCH',
    desc:  'Return the strategy to research mode. Paper trading will stop.',
    requiresUnderstand: false,
    usesPromotePaper:   false,
    targetState:        'RESEARCH',
  },
  retire: {
    label: 'Demote to RETIRED',
    desc:  'Retire this strategy permanently. It will no longer trade in any mode.',
    requiresUnderstand: true,
    usesPromotePaper:   false,
    targetState:        'RETIRED',
  },
  revive: {
    label: 'Revive to RESEARCH',
    desc:  'Restore this retired strategy to research mode.',
    requiresUnderstand: false,
    usesPromotePaper:   false,
    targetState:        'RESEARCH',
  },
  rollback_paper: {
    label: 'Soft rollback to PAPER',
    desc:  'Move the strategy from LIVE to PAPER. Existing live positions are unaffected.',
    requiresUnderstand: false,
    usesPromotePaper:   false,
    targetState:        'PAPER',
  },
}

// ── Lifecycle state badge helper ────────────────────────────────────

function lcStateBadge(state: LifecycleState): string {
  switch (state) {
    case 'RESEARCH': return 'bg-blue-500/15 text-blue-400 border-blue-500/30'
    case 'PAPER':    return 'bg-amber-500/15 text-amber-400 border-amber-500/30'
    case 'LIVE':     return 'bg-green-500/15 text-green-400 border-green-500/30'
    case 'RETIRED':  return 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30'
  }
}

// ── Gate failure display ────────────────────────────────────────────

function GateFailures({ resp }: { resp: PromotionResponse }) {
  const failed = resp.gates
    ? Object.entries(resp.gates).filter(([, v]) => !v).map(([k]) => k)
    : []
  return (
    <div className="bg-red-500/10 border border-red-500/30 rounded p-3 text-sm text-red-400 space-y-1">
      <div className="font-medium">⚠ Promotion blocked</div>
      <div>{resp.reason ?? 'Gate checks failed'}</div>
      {failed.length > 0 && (
        <div className="text-xs">Failed gates: <span className="font-mono">{failed.join(', ')}</span></div>
      )}
      {resp.paper_sharpe != null && (
        <div className="text-xs text-[var(--color-text-muted)]">
          Paper σ {fmtNum(resp.paper_sharpe, 2)}
          {resp.research_sharpe != null && ` · Research σ ${fmtNum(resp.research_sharpe, 2)}`}
          {resp.gap != null && ` · Gap ${fmtNum(resp.gap, 2)}`}
        </div>
      )}
    </div>
  )
}

// ── Per-action metrics display ──────────────────────────────────────

function ActionMetrics({ action, row }: { action: LifecycleActionType; row: LifecycleRow }) {
  const m = (v: number | null | undefined, digits = 2) =>
    v != null ? fmtNum(v, digits) : '—'

  const gapClass = (gap: number | null) => {
    if (gap == null) return 'text-[var(--color-text-muted)]'
    if (gap > 0.5) return 'text-red-400'
    if (gap > 0.3) return 'text-amber-400'
    return 'text-green-400'
  }

  switch (action) {
    case 'promote_paper':
      return (
        <div className="text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-alt)] rounded p-2">
          Research σ <span className="font-mono text-[var(--color-text)]">{m(row.research_sharpe)}</span>
        </div>
      )
    case 'promote_live':
      return (
        <div className="text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-alt)] rounded p-2 space-y-0.5">
          <div>
            Paper σ <span className="font-mono text-[var(--color-text)]">{m(row.paper_sharpe)}</span>
            {' · '}Gap{' '}
            <span className={`font-mono ${gapClass(row.gap)}`}>{m(row.gap)}</span>
          </div>
          <div>
            Paper trades <span className="font-mono text-[var(--color-text)]">{m(row.paper_trades_count, 0)}</span>
            {' · '}Days in paper <span className="font-mono text-[var(--color-text)]">{m(row.days_in_paper, 0)}</span>
          </div>
        </div>
      )
    case 'rollback':
      return (
        <div className="text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-alt)] rounded p-2">
          Paper σ <span className="font-mono text-[var(--color-text)]">{m(row.paper_sharpe)}</span>
          {' · '}Gap <span className={`font-mono ${gapClass(row.gap)}`}>{m(row.gap)}</span>
        </div>
      )
    case 'retire':
    case 'rollback_paper':
      return (
        <div className="text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-alt)] rounded p-2">
          Live σ <span className="font-mono text-[var(--color-text)]">{m(row.live_sharpe)}</span>
          {' · '}Live trades <span className="font-mono text-[var(--color-text)]">{m(row.live_trades_count, 0)}</span>
        </div>
      )
    default:
      return null
  }
}

// ── Public types ─────────────────────────────────────────────────────

export interface LifecycleTransitionModalProps {
  open: boolean
  onClose: () => void
  action: LifecycleActionType
  row: LifecycleRow
  /** Called after a successful transition so parent can invalidate cache. */
  onSuccess: () => void
}

// ── Component ────────────────────────────────────────────────────────

export function LifecycleTransitionModal({
  open,
  onClose,
  action,
  row,
  onSuccess,
}: LifecycleTransitionModalProps) {
  const cfg = ACTION_CFG[action]

  // ── Local state ─────────────────────────────────────────────────
  const [reason,          setReason]          = useState('')
  const [iUnderstand,     setIUnderstand]     = useState(false)
  const [busy,            setBusy]            = useState(false)
  const [error,           setError]           = useState<string | null>(null)
  const [disallowedMsg,   setDisallowedMsg]   = useState<string | null>(null)
  const [forceOverride,   setForceOverride]   = useState(false)
  const [gateFailure,     setGateFailure]     = useState<PromotionResponse | null>(null)
  const [success,         setSuccess]         = useState(false)

  const reasonRef = useRef<HTMLTextAreaElement>(null)

  // ── Auto-focus reason textarea on mount ─────────────────────────
  // NOTE: No reset useEffect needed — this component is conditionally rendered
  // and unmounts (activeAction=null) on close, giving fresh state each open.
  useEffect(() => {
    const t = setTimeout(() => reasonRef.current?.focus(), 50)
    return () => clearTimeout(t)
  }, [])

  // ── ESC to close ─────────────────────────────────────────────────
  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !busy) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, busy, onClose])

  // ── Submit gate ──────────────────────────────────────────────────
  const canSubmit = useMemo<boolean>(() => {
    if (reason.length < 10 || reason.length > 500) return false
    if (cfg.requiresUnderstand && !iUnderstand) return false
    if (busy) return false
    return true
  }, [reason, cfg.requiresUnderstand, iUnderstand, busy])

  // ── Submit handler ────────────────────────────────────────────────
  async function handleSubmit() {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    setDisallowedMsg(null)
    setGateFailure(null)

    try {
      if (cfg.usesPromotePaper) {
        // promote_live: POST /promote-paper (gates enforced by backend)
        // Note: reason is for operator record-keeping; backend doesn't accept it in body per API spec
        const resp = await promotePaper(row.strategy, row.universe)
        if (!resp.promoted) {
          setGateFailure(resp)
          setBusy(false)
          return
        }
      } else {
        // All other actions: POST /transition
        await lifecycleTransition({
          strategy:  row.strategy,
          universe:  row.universe,
          new_state: cfg.targetState,
          reason,
          ...(forceOverride ? { force: true } : {}),
        })
      }
      setSuccess(true)
      onSuccess()
      setTimeout(() => onClose(), 2000)
    } catch (e: unknown) {
      const isApiError = e instanceof ApiError
      const status     = isApiError ? e.status : 0
      const msg        = isApiError ? e.detail  : (e instanceof Error ? e.message : String(e))

      if (status === 400 && msg.includes('Disallowed')) {
        setDisallowedMsg(msg)
      } else {
        setError(msg)
      }
      setBusy(false)
    }
  }

  // ── Early exit ────────────────────────────────────────────────────
  if (!open) return null

  // ── Success state ─────────────────────────────────────────────────
  if (success) {
    return (
      <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-8 shadow-2xl flex flex-col items-center gap-4 max-w-md w-full">
          <div className="text-5xl text-green-400">✓</div>
          <div className="text-base font-mono text-green-400 text-center">
            {row.strategy} · {row.universe} → {cfg.targetState}
          </div>
        </div>
      </div>
    )
  }

  // ── Normal modal ──────────────────────────────────────────────────
  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={e => { if (!busy && e.target === e.currentTarget) onClose() }}
    >
      <div
        className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 shadow-2xl max-w-md md:max-w-lg w-full"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-2 mb-4">
          <div className="min-w-0">
            <h2 className="text-base font-semibold leading-snug">{cfg.label}</h2>
            <div className="mt-1 text-xs text-[var(--color-text-muted)] font-mono">
              {row.strategy} · {row.universe}
            </div>
            <div className="mt-1 flex items-center gap-1.5 flex-wrap text-xs text-[var(--color-text-muted)]">
              <span>Current:</span>
              <span
                className={`px-1.5 py-0.5 rounded font-mono border text-[10px] ${lcStateBadge(row.state)}`}
                data-testid="lifecycle-current-state"
              >
                {row.state}
              </span>
              <span>→</span>
              <span
                className={`px-1.5 py-0.5 rounded font-mono border text-[10px] ${lcStateBadge(cfg.targetState)}`}
                data-testid="lifecycle-target-state"
              >
                {cfg.targetState}
              </span>
            </div>
          </div>
          <button
            type="button"
            onClick={() => { if (!busy) onClose() }}
            className="flex-shrink-0 text-[var(--color-text-muted)] hover:text-[var(--color-text)] text-xl leading-none mt-0.5"
            aria-label="Close modal"
          >
            ×
          </button>
        </div>

        <div className="space-y-4">
          {/* Description */}
          <p className="text-sm text-[var(--color-text-muted)]">{cfg.desc}</p>

          {/* Per-action metrics */}
          <ActionMetrics action={action} row={row} />

          {/* Gate failure display (promote_live only) */}
          {gateFailure && <GateFailures resp={gateFailure} />}

          {/* Reason textarea — always shown; note for promote_live: not sent to backend */}
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">
              Reason (required, ≥10 chars)
              {action === 'promote_live' && (
                <span className="ml-1 normal-case">(for operator audit log)</span>
              )}
            </label>
            <textarea
              ref={reasonRef}
              rows={3}
              value={reason}
              onChange={e => setReason(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') e.stopPropagation() }}
              placeholder="Describe why you are making this change…"
              className="w-full bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded p-2 text-sm font-mono resize-none focus:outline-none focus:ring-1 focus:ring-[var(--color-border)]"
            />
            <div className={`text-xs mt-0.5 text-right tabular-nums ${charCountClass(reason.length)}`}>
              {reason.length} / 500
            </div>
          </div>

          {/* "I understand" checkbox — retire only */}
          {cfg.requiresUnderstand && (
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={iUnderstand}
                onChange={e => setIUnderstand(e.target.checked)}
                data-testid="i-understand-checkbox"
              />
              <span className="text-sm text-amber-400">
                I understand this permanently retires the strategy.
              </span>
            </label>
          )}

          {/* Force-override section — shown after "Disallowed" 400 */}
          {disallowedMsg && (
            <div
              className="bg-red-500/10 border border-red-500/30 rounded p-3 space-y-2"
              data-testid="force-override-section"
            >
              <div className="text-sm text-red-400">{disallowedMsg}</div>
              <label
                className="flex items-center gap-2 cursor-pointer select-none"
              >
                <input
                  type="checkbox"
                  checked={forceOverride}
                  onChange={e => setForceOverride(e.target.checked)}
                  data-testid="force-override-checkbox"
                />
                <span className="text-sm text-amber-400">
                  Override anyway (force=true)
                </span>
              </label>
            </div>
          )}

          {/* Generic error banner */}
          {error !== null && (
            <div className="bg-red-500/10 border border-red-500/30 rounded p-2 text-xs text-red-400">
              {error}
            </div>
          )}

          {/* Action row */}
          <div className="flex items-center justify-end gap-3 pt-2 border-t border-[var(--color-border)]">
            <button
              type="button"
              disabled={busy}
              onClick={() => { if (!busy) onClose() }}
              className="text-[var(--color-text-muted)] hover:text-[var(--color-text)] px-4 py-2 text-sm disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!canSubmit}
              onClick={() => { void handleSubmit() }}
              data-testid="lifecycle-submit-btn"
              className={[
                'px-4 py-2 rounded text-sm font-medium',
                'disabled:opacity-40 disabled:cursor-not-allowed',
                action === 'retire'
                  ? 'bg-red-500/15 text-red-400 border border-red-500/30 hover:bg-red-500/25'
                  : 'bg-green-500/15 text-green-400 border border-green-500/30 hover:bg-green-500/25',
              ].join(' ')}
            >
              {busy ? (
                <span className="flex items-center gap-1.5">
                  <span className="inline-block animate-spin">⟳</span>
                  Submitting…
                </span>
              ) : cfg.label}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
