// Phase 2 dogfood feature flags — read at build time from Vite env vars.
// To enable Controls tab in dev:    VITE_ENABLE_CONTROLS_TAB=true npm run dev
// To enable Controls tab in build:  VITE_ENABLE_CONTROLS_TAB=true npm run build
// Default: OFF in production builds (per spec §10 Phase 2 — dogfood gated).

const TRUTHY = new Set(['true', '1', 'yes', 'on'])

function flag(name: string, defaultValue: boolean = false): boolean {
  const v = (import.meta.env[name] as string | undefined)?.toLowerCase()
  if (v == null) return defaultValue
  return TRUTHY.has(v)
}

export const FEATURE_CONTROLS_TAB = flag('VITE_ENABLE_CONTROLS_TAB', false)
