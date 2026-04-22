export const STRATEGY_COLORS: Record<string, string> = {
  MR: '#6366f1',
  TF: '#22c55e',
  OG: '#f59e0b',
  MB: '#ec4899',
  SR: '#14b8a6',
  STMR: '#a855f7',
  ConnorsRSI2: '#ef4444',
  connors_rsi2: '#ef4444',
  mean_reversion: '#6366f1',
  trend_following: '#22c55e',
  opening_gap: '#f59e0b',
  momentum_breakout: '#ec4899',
  support_resistance: '#14b8a6',
  short_term_mr: '#a855f7',
}

// New 6-state regime taxonomy (canonical keys)
export const REGIME_COLORS: Record<string, string> = {
  bull_risk_on:         '#16a34a',   // bright green
  bull_risk_off:        '#65a30d',   // olive green
  transition_uncertain: '#f59e0b',   // amber
  bear_risk_off:        '#dc2626',   // red
  bear_capitulation:    '#7f1d1d',   // dark red
  recovery_early:       '#0ea5e9',   // sky blue

  // Legacy aliases — old DB rows may still have these strings
  bull_quiet:           '#65a30d',   // → bull_risk_off color
  bull_volatile:        '#16a34a',   // → bull_risk_on color
  bear_quiet:           '#dc2626',   // → bear_risk_off color
  bear_volatile:        '#7f1d1d',   // → bear_capitulation color
  neutral:              '#a1a1aa',   // grey (no equivalent in new schema)
}

// The canonical new-schema names (used to filter legend)
export const CANONICAL_REGIME_NAMES = [
  'bull_risk_on',
  'bull_risk_off',
  'transition_uncertain',
  'bear_risk_off',
  'bear_capitulation',
  'recovery_early',
] as const

export function getStrategyColor(name: string | null | undefined): string {
  if (!name) return '#a1a1aa'
  return STRATEGY_COLORS[name] ?? STRATEGY_COLORS[String(name).toLowerCase()] ?? '#a1a1aa'
}

export function getRegimeColor(state: string | null | undefined): string {
  if (!state) return '#a1a1aa'
  return REGIME_COLORS[state] ?? REGIME_COLORS[String(state).toLowerCase()] ?? '#a1a1aa'
}
