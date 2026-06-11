import { useEffect, useRef, useState } from 'react'
import { useReducedMotion } from './useReducedMotion'

const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3)

/**
 * Animate a number toward `target`: first mount counts up from 0, subsequent
 * changes glide from the previous value (so 60s polls nudge, not re-count).
 * Returns `target` directly under reduced motion or when null/undefined.
 */
export function useCountUp(
  target: number | null | undefined,
  opts?: { duration?: number },
): number | null {
  const duration = opts?.duration ?? 800
  const reduced = useReducedMotion()
  const [display, setDisplay] = useState<number | null>(target ?? null)
  const fromRef = useRef<number | null>(null) // null until first animation ran
  const rafRef = useRef<number>(0)

  useEffect(() => {
    const from = fromRef.current ?? 0 // first mount: from zero
    if (target == null || reduced || from === target) {
      fromRef.current = target ?? null
      // settle via rAF (never set state synchronously inside an effect)
      rafRef.current = requestAnimationFrame(() => setDisplay(target ?? null))
      return () => cancelAnimationFrame(rafRef.current)
    }
    const start = performance.now()
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      const v = from + (target - from) * easeOutCubic(t)
      setDisplay(t >= 1 ? target : v)
      if (t < 1) rafRef.current = requestAnimationFrame(tick)
      else fromRef.current = target
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(rafRef.current)
      fromRef.current = target // interrupted: next run glides from the goal we were heading to
    }
  }, [target, duration, reduced])

  return display
}
