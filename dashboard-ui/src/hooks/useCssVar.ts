import { useMemo } from 'react'
import { useTheme } from './useTheme'

/**
 * Read a CSS custom property from :root.
 * Re-evaluated whenever the theme toggles (dark ↔ light) because the CSS
 * variable resolves to a different value under the html.light scope.
 *
 * Rule: rerender-derived-state-no-effect — no useState + useEffect round-trip;
 * we derive the value directly inside useMemo on each theme change.
 */
export function useCssVar(name: string): string {
  const { theme } = useTheme()
  return useMemo(() => {
    if (typeof window === 'undefined') return ''
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    // theme is intentionally in the deps to re-read after a toggle
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, theme])
}

/**
 * Batch variant of useCssVar — reads multiple CSS custom properties in a
 * single getComputedStyle call, reducing DOM reads to one.
 *
 * Rules: js-batch-dom-css — single getComputedStyle invocation for all vars;
 *        js-cache-function-results — result memoized until names or theme change.
 *
 * SSR-safe: returns empty strings for all keys when window is undefined.
 * Caller should pass a stable reference (e.g. `as const` array literal) to
 * avoid unnecessary recomputation.
 */
export function useCssVars<T extends string>(names: readonly T[]): Record<T, string> {
  const { theme } = useTheme()
  return useMemo(() => {
    if (typeof window === 'undefined') {
      return Object.fromEntries(names.map((n) => [n, ''])) as Record<T, string>
    }
    const style = getComputedStyle(document.documentElement)
    return Object.fromEntries(
      names.map((n) => [n, style.getPropertyValue(n).trim()])
    ) as Record<T, string>
    // names reference assumed stable (as const literal); theme triggers re-read on toggle
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [names, theme])
}
