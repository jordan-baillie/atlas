/**
 * useUrlState -- two-way bind a single URL query-param to React state.
 *
 * Atlas's dashboard doesn't use react-router; this is a small hook that
 * uses window.location + history.pushState + a popstate listener so a
 * specific query param (e.g. `?stage=specs_extracted`) survives reload
 * and back/forward navigation.
 *
 * Usage:
 *     const [stage, setStage] = useUrlState<StageKey | null>('stage', null)
 *
 * Notes:
 *   - All bound params live in the same querystring, so multiple hooks
 *     on different keys coexist.
 *   - When the new value === defaultValue (or null), the param is
 *     removed from the URL rather than set to "null".
 *   - Same-tab updates fire a `urlstate` CustomEvent so other hook
 *     instances can resync immediately without waiting for popstate.
 */

import { useCallback, useEffect, useState } from 'react'

const SYNC_EVENT = 'urlstate-sync'

function readParam(key: string): string | null {
  if (typeof window === 'undefined') return null
  const p = new URLSearchParams(window.location.search)
  return p.get(key)
}

function writeParam(key: string, value: string | null): void {
  const url = new URL(window.location.href)
  if (value === null || value === '') {
    url.searchParams.delete(key)
  } else {
    url.searchParams.set(key, value)
  }
  // Replace rather than push -- we don't want every dropdown change to
  // create a back-history entry.  Use pushState only for navigation-like
  // transitions if needed in the future.
  window.history.replaceState(window.history.state, '', url.toString())
  window.dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: { key, value } }))
}

export function useUrlState<T extends string | null>(
  key: string,
  defaultValue: T,
): [T, (value: T) => void] {
  const [value, setLocal] = useState<T>(() => {
    const raw = readParam(key)
    return (raw === null ? defaultValue : (raw as T))
  })

  useEffect(() => {
    function onChange() {
      const raw = readParam(key)
      setLocal((raw === null ? defaultValue : (raw as T)))
    }
    window.addEventListener('popstate', onChange)
    window.addEventListener(SYNC_EVENT, onChange as EventListener)
    return () => {
      window.removeEventListener('popstate', onChange)
      window.removeEventListener(SYNC_EVENT, onChange as EventListener)
    }
  }, [key, defaultValue])

  const set = useCallback((next: T) => {
    setLocal(next)
    writeParam(key, next === defaultValue ? null : (next as string | null))
  }, [key, defaultValue])

  return [value, set]
}
