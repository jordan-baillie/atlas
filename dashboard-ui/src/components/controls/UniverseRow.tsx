import { useState } from 'react'
import { useChangeUniverseState } from '../../api/admin-queries'
import { ChangeStateModal } from './ChangeStateModal'
import { RevertButton } from './RevertButton'
import { Badge } from '../shared/Badge'
import { fmtCcy, fmtRelativeTime } from '../../lib/format'
import type { UniverseAdminRow } from '../../api/admin-types'
import type { BadgeVariant } from '../shared/Badge'

// ── Helpers ───────────────────────────────────────────────────────────────

function stateVariant(state: string): BadgeVariant {
  if (state === 'live') return 'success'
  if (state === 'passive') return 'warning'
  return 'neutral'
}

function stateLabel(state: string): string {
  if (state === 'live') return 'LIVE'
  if (state === 'passive') return 'PASSIVE'
  if (state === 'disabled') return 'DISABLED'
  return state.toUpperCase()
}

// ── Component ─────────────────────────────────────────────────────────────

export function UniverseRow({ row }: { row: UniverseAdminRow }) {
  const [open, setOpen] = useState(false)
  const mutation = useChangeUniverseState()

  const overrideExpiringSoon = row.override?.expires_at
    ? (new Date(row.override.expires_at).getTime() - Date.now()) < 7 * 24 * 3600 * 1000
    : false

  const isProduction = row.effective_state === 'live'

  async function handleSubmit(req: {
    state: string
    reason: string
    expires_at: string | null | undefined
    confirm_token?: string
    i_understand: boolean
  }) {
    await mutation.mutateAsync({
      market_id: row.market_id,
      body: {
        state: req.state as 'live' | 'passive' | 'disabled',
        reason: req.reason,
        expires_at: req.expires_at,
        confirm_token: req.confirm_token,
        i_understand: req.i_understand,
      },
    })
  }

  return (
    <div className="border border-[var(--color-border)] rounded-lg p-3 flex items-center justify-between gap-3 flex-wrap hover:bg-[var(--color-surface-alt)]/30 transition-colors">

      {/* Left: identity + state */}
      <div className="flex items-center gap-3 flex-wrap">
        <span className="font-mono text-sm font-semibold min-w-[140px]">{row.market_id}</span>

        {/* Effective state badge */}
        <Badge variant={stateVariant(row.effective_state)} size="sm" dot>
          {stateLabel(row.effective_state)}
        </Badge>

        {/* Source badge — override or config */}
        {row.override ? (
          <Badge
            variant={overrideExpiringSoon ? 'warning' : 'neutral'}
            size="xs"
            title={`Reason: ${row.override.reason ?? '—'}\nBy: ${row.override.created_by}\nAt: ${row.override.created_at}\nExpires: ${row.override.expires_at ?? 'never'}`}
          >
            override{' '}
            {row.override.expires_at
              ? `exp ${row.override.expires_at.slice(0, 10)}`
              : 'permanent'}
          </Badge>
        ) : (
          <Badge variant="neutral" size="xs">config</Badge>
        )}
      </div>

      {/* Right: metrics + actions */}
      <div className="flex items-center gap-3 text-xs text-[var(--color-text-muted)] flex-wrap">
        <span title="Open positions" className="tabular-nums font-mono">
          {row.open_positions} pos
        </span>
        <span className="tabular-nums font-mono">
          {row.current_equity != null ? fmtCcy(row.current_equity) : '—'}
        </span>
        <span title="Last trade" className="font-mono">
          {row.last_trade_at ? fmtRelativeTime(row.last_trade_at) : 'no trades'}
        </span>

        {row.override && <RevertButton overrideId={row.override.id} />}

        <button
          onClick={() => setOpen(true)}
          className="h-8 px-3 rounded-md bg-[var(--color-surface-alt)] hover:bg-[var(--color-border)]
                     border border-[var(--color-border)] text-xs text-[var(--color-text-muted)]
                     hover:text-[var(--color-text)] transition-colors"
        >
          Change ▾
        </button>
      </div>

      <ChangeStateModal
        open={open}
        onClose={() => setOpen(false)}
        scope="universe"
        marketId={row.market_id}
        currentState={row.effective_state}
        currentSource={row.override ? 'override' : 'config'}
        isProduction={isProduction}
        openPositions={row.open_positions}
        onSubmit={handleSubmit}
      />
    </div>
  )
}
