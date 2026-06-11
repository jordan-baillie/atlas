import { useEffect, useRef, useState } from 'react'
import { useReducedMotion } from './useReducedMotion'

export interface Celebration {
  /** True while the one-shot celebration effect should render. */
  celebrating: boolean
  /** Reduced motion: caller should show a static highlight instead. */
  reduced: boolean
}

/**
 * One-shot celebration keyed on a localStorage watermark.
 * When `stamp` is non-null and differs from the stored watermark, the
 * watermark is consumed IMMEDIATELY (so Command + Forge never double-fire
 * for the same event) and `celebrating` is true for `durationMs`.
 */
export function useCelebration(key: string, stamp: string | null | undefined, durationMs = 2600): Celebration {
  const reduced = useReducedMotion()
  const [celebrating, setCelebrating] = useState(false)
  const firedRef = useRef(false)

  useEffect(() => {
    if (!stamp || firedRef.current) return
    const storageKey = `atlas-celebrate:${key}`
    let seen: string | null = null
    try {
      seen = localStorage.getItem(storageKey)
    } catch {
      /* storage unavailable — celebrate anyway, just never persist */
    }
    if (seen === stamp) return
    try {
      localStorage.setItem(storageKey, stamp) // consume-on-fire
    } catch {
      /* ignore */
    }
    firedRef.current = true
    const raf = requestAnimationFrame(() => setCelebrating(true))
    const t = setTimeout(() => setCelebrating(false), durationMs)
    return () => {
      cancelAnimationFrame(raf)
      clearTimeout(t)
    }
  }, [key, stamp, durationMs])

  return { celebrating, reduced }
}
