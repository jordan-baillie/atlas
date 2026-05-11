import { useState } from 'react'
import { useRevertOverride } from '../../api/admin-queries'
import { ApiError } from '../../api/client'

interface Props {
  overrideId: number
  label?: string   // default "Revert"
  onSuccess?: () => void
}

// Shared button base for consistent height (32px)
const BTN_BASE =
  'h-8 px-2.5 rounded-md text-xs border transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

export function RevertButton({ overrideId, label = 'Revert', onSuccess }: Props) {
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mutation = useRevertOverride()

  async function doRevert() {
    setError(null)
    try {
      await mutation.mutateAsync({
        override_id: overrideId,
        body: { reason: 'Reverted via dashboard one-click button' },
      })
      setConfirming(false)
      onSuccess?.()
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message)
    }
  }

  if (confirming) {
    return (
      <span className="inline-flex items-center gap-2">
        <button
          onClick={() => void doRevert()}
          disabled={mutation.isPending}
          className={`${BTN_BASE} bg-amber-500/10 text-amber-400 border-amber-500/30 hover:bg-amber-500/20`}
        >
          {mutation.isPending ? '…' : `Confirm ${label.toLowerCase()}?`}
        </button>
        <button
          onClick={() => setConfirming(false)}
          disabled={mutation.isPending}
          className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
        >
          cancel
        </button>
        {error && <span className="text-xs text-[var(--color-red)]">{error}</span>}
      </span>
    )
  }

  return (
    <button
      onClick={() => setConfirming(true)}
      className={`${BTN_BASE} bg-[var(--color-surface-alt)] text-[var(--color-text-muted)] border-[var(--color-border)] hover:bg-[var(--color-border)] hover:text-[var(--color-text)]`}
    >
      ↺ {label}
    </button>
  )
}
