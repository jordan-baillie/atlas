import { useEffect, useRef, useState } from 'react'
import { useReducedMotion } from './useReducedMotion'

export interface DeltaFlash {
  direction: 'up' | 'down' | null
  /** Monotonic id — key a span on this so the CSS animation re-triggers. */
  flashId: number
}

/** Fire a 700ms 'up'/'down' signal whenever `value` actually changes. */
export function useDeltaFlash(value: number | null | undefined): DeltaFlash {
  const reduced = useReducedMotion()
  const prevRef = useRef<number | null | undefined>(undefined)
  const [state, setState] = useState<DeltaFlash>({ direction: null, flashId: 0 })

  useEffect(() => {
    const prev = prevRef.current
    prevRef.current = value
    if (reduced || prev === undefined || prev == null || value == null || value === prev) return
    const direction: 'up' | 'down' = value > prev ? 'up' : 'down'
    const raf = requestAnimationFrame(() => {
      setState((s) => ({ direction, flashId: s.flashId + 1 }))
    })
    const t = setTimeout(() => setState((s) => ({ ...s, direction: null })), 700)
    return () => {
      cancelAnimationFrame(raf)
      clearTimeout(t)
    }
  }, [value, reduced])

  return state
}
