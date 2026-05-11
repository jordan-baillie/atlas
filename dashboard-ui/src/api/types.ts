// ============================================================================
// /api/dashboard-data — Main portfolio snapshot
// All shapes verified against live API response on 2026-04-09
// ============================================================================

export interface Account {
  equity?: number
  cash?: number
  market_value?: number
  buying_power?: number
  total_pnl?: number
  total_pnl_pct?: number
  num_positions?: number
  currency?: string
  market_id?: string
  halted?: boolean
  halt_reason?: string
  margin_usage_pct?: number
}

export interface Position {
  ticker?: string
  strategy?: string
  entry_date?: string
  entry_price?: number
  shares?: number
  current_price?: number
  market_value?: number
  unrealized_pnl?: number
  unrealized_pnl_pct?: number
  stop_price?: number | null
  take_profit?: number | null
  cost_basis?: number
  sector?: string
  today_pnl?: number
  currency?: string
  intraday_pnl?: number
  intraday_pnl_pct?: number
  lastday_price?: number
}

export interface Order {
  success?: boolean
  order_id?: string
  ticker?: string
  side?: 'buy' | 'sell'
  status?: string
  requested_qty?: number
  filled_qty?: number
  requested_price?: number
  fill_price?: number
  commission?: number
  message?: string
  symbol?: string
  type?: string
  qty?: number
  submitted_at?: string
  limit_price?: number
  stop_price?: number
  trail_price?: number
  filled_price?: number
}

export interface EquityPoint {
  date?: string
  equity?: number
  day_pnl?: number
}

export interface BenchmarkCurvePoint {
  date?: string
  equity?: number
}

export interface Benchmark {
  ticker?: string
  curve?: BenchmarkCurvePoint[]
  return_pct?: number
}

export interface StrategyPerfEntry {
  trades?: number
  pnl?: number
  wins?: number
  // Phase 7: EV scoring (optional, populated when joined with signal_ev table)
  ev_per_trade?: number
  ev_classification?: 'positive' | 'negative' | 'uncertain' | 'unknown'
}

export interface StrategyPerfOverall {
  trades?: number
  win_rate?: number
  avg_win?: number
  avg_loss?: number
  profit_factor?: number
  expectancy?: number
}

export interface StrategyPerformance {
  by_strategy?: Record<string, StrategyPerfEntry>
  overall?: StrategyPerfOverall
}

export interface StrategyAllocation {
  strategy?: string
  value?: number
  pct?: number
  positions?: number
}

export interface DashboardSummary {
  equity?: number
  total_pnl?: number
  total_pnl_pct?: number
  return_pct?: number          // window-aligned return (matches portfolio_history span)
  open_positions?: number
  today_pnl?: number
  max_positions?: number
}

export interface MarketClock {
  is_open?: boolean
  next_open?: string
  next_close?: string
  timestamp?: string
}

export interface DashboardData {
  account?: Account
  positions?: Position[]
  recent_orders?: Order[]
  summary?: DashboardSummary
  market_clock?: MarketClock
  portfolio_history?: EquityPoint[]
  strategy_performance?: StrategyPerformance
  benchmark?: Benchmark
  strategy_allocation?: StrategyAllocation[]
  timestamp?: string
}

// ============================================================================
// /api/regime/current and /api/regime/history
// ============================================================================

export interface RegimeCurrent {
  state?: string
  label?: string
  confidence?: number
  days_in_state?: number
  as_of?: string
  probabilities?: Record<string, number>
}

export interface RegimeHistoryDay {
  date?: string
  state?: string
  confidence?: number
}

export type RegimeHistory = RegimeHistoryDay[]

// ============================================================================
// /api/regime/transitions
// ============================================================================

export interface RegimeDuration {
  avg_days?: number
  max_days?: number
  occurrences?: number
  total_days?: number
}

export interface RegimeTransitions {
  matrix?: Record<string, Record<string, number>>
  durations?: Record<string, RegimeDuration>
  states?: string[]
  current_state?: string
  total_days?: number
  as_of?: string
  stale?: boolean
  reason?: string
}

// ============================================================================
// /api/system/health
// ============================================================================

// Legacy — kept for backward compat with any remaining importers
export interface HealthService {
  name?: string
  status?: string
  uptime?: string | number
  last_check?: string
  error?: string
}

export interface HealthCronJob {
  name?: string
  schedule?: string
  last_run?: string
  next_run?: string
  status?: string
  exit_code?: number
}

// Legacy — kept for backward compat
export interface DataFreshness {
  table?: string
  name?: string
  last_updated?: string
  rows?: number
  age_minutes?: number
  status?: string
}

export interface HealthHeartbeat {
  service?: string
  timestamp?: string
  status?: string
  detail?: unknown
}

export interface HealthDataFreshness {
  ohlcv_last_date?: string
  ohlcv_last_session?: string  // last NYSE trading session date (YYYY-MM-DD) — from backend
  ohlcv_is_fresh?: boolean     // true when ohlcv_last_date >= ohlcv_last_session
  equity_last_date?: string
  overlay_decisions_count?: number
}

export interface SystemHealth {
  services?: Record<string, string>
  cron?: Record<string, HealthCronJob>
  data_freshness?: HealthDataFreshness
  heartbeats?: HealthHeartbeat[]
  overall?: string
  timestamp?: string
}

// ============================================================================
// /api/macro/gauges
// ============================================================================

export interface MacroDimension {
  name?: string
  label?: string
  score?: number
  raw_label?: string
  raw_value?: string
  raw_detail?: string
  sparkline?: number[]
  weight?: number
}

export interface MacroGaugeData {
  dimensions?: MacroDimension[]
  composite?: number
  available_weight?: number
  date?: string
}

// ============================================================================
// /api/positions/risk
// ============================================================================

export interface PositionRiskRow {
  ticker?: string
  strategy?: string
  shares?: number
  entry_price?: number
  current_price?: number
  stop_price?: number
  has_stop?: boolean
  distance_pct?: number
  distance_dollars?: number
  max_loss?: number
  risk_pct_equity?: number
  position_value?: number
  risk_status?: string
  vol_cone?: VolConeInfo | null
}

export interface PositionRiskSummary {
  total_risk_dollars?: number
  total_risk_pct?: number
  equity?: number
  num_positions?: number
  avg_distance_to_stop?: number
  positions_without_stops?: number
  max_risk_per_trade_pct?: number
}

export interface VolConeInfo {
  vol_20d_annual?: number
  regime?: 'low' | 'normal' | 'high' | 'extreme' | string
  percentile?: number
  multiplier?: number
  suggested_stop_distance_pct?: number
}

export interface VarHorizonMetrics {
  var_95?: number
  var_99?: number
  cvar_95?: number
  cvar_99?: number
  var_95_pct?: number
}

export interface PortfolioRiskMetrics {
  method?: string
  current_regime?: string
  effective_bets?: number
  correlation_avg?: number
  correlation_max?: number
  horizons?: Record<string, VarHorizonMetrics>
  n_paths?: number
  warnings?: string[]
}

export interface StopProbabilityEntry {
  vol_annual: number
  stop_distance_pct: number
  horizons: { '1d': number; '5d': number; '10d': number; '20d': number }
  expected_loss_20d: number
  max_loss: number
}

export interface PositionRisk {
  positions?: PositionRiskRow[]
  summary?: PositionRiskSummary
  portfolio_risk?: PortfolioRiskMetrics | null
  vol_cones?: Record<string, VolConeInfo>
  stop_probability?: Record<string, StopProbabilityEntry>
  as_of?: string
  stale?: boolean
  reason?: string
}

// ============================================================================
// /api/finance
// ============================================================================

export interface FinanceNetWorth {
  total_aud?: number
  up_bank_aud?: number
  atlas_equity_usd?: number
  atlas_equity_aud?: number
  moomoo_equity_aud?: number
  moomoo_last_updated?: string
  invested_aud?: number
  aud_usd_rate?: number
  pct_invested?: number
  pct_cash?: number
}

export interface FinanceAccount {
  name?: string
  type?: string
  balance?: number
  limit?: number | null
}

export interface SpendCategory {
  category?: string
  label?: string
  amount?: number
}

export interface MonthlySpending {
  period?: string
  total?: number
  by_parent_category?: SpendCategory[]
  by_category?: SpendCategory[]
}

export interface SpendingTrendPoint {
  date?: string
  spending?: number
  cumulative?: number
}

export interface RecentTransaction {
  date?: string
  description?: string
  amount?: number
  category?: string
  parent_category?: string
  method?: string
}

export interface BalanceHistoryPoint {
  date?: string
  up_total?: number
  atlas_aud?: number
  moomoo_aud?: number
  net_worth?: number
}

export interface SavingsRate {
  income_this_month?: number
  spending_this_month?: number
  rate_pct?: number
}

export interface PortfolioSnapshot {
  equity_usd?: number
  equity_aud?: number
  total_pnl_usd?: number
  total_pnl_aud?: number
  positions?: number
  fresh?: boolean
  last_updated?: string
}

export interface FinancePortfolios {
  atlas?: PortfolioSnapshot
  moomoo?: PortfolioSnapshot
}

export interface InvestmentAllocation {
  atlas_pct?: number
  moomoo_pct?: number
}

export interface FinancePerformance {
  income_aud?: number
  combined_return_aud?: number
  atlas_return_aud?: number
  moomoo_return_aud?: number
  monthly_spending_aud?: number
  net_progress_aud?: number
  savings_aud?: number
  runway_months?: number
  fi_ratio_pct?: number
  investment_allocation?: InvestmentAllocation
}

export interface MonthlyComparison {
  month?: string
  spending?: number
  income?: number
  net?: number
}

export interface TopCategory {
  category?: string
  label?: string
  amount?: number
  count?: number
}

export interface TopMerchant {
  merchant?: string
  total?: number
  count?: number
  avg?: number
}

export interface RecurringItem {
  merchant?: string
  frequency?: string
  avg_amount?: number
  total_90d?: number
  est_monthly?: number
}

export interface PacePoint {
  date?: string
  actual?: number
  budget?: number
}

export interface PaceHistoryEntry {
  month?: string
  label?: string
  pace_data?: PacePoint[]
  pace_status?: string
  pace_diff?: number
  total_budget?: number
  total_spent?: number
  income?: number
}

export interface DayOfWeekSpending {
  day?: string
  total?: number
  count?: number
}

export interface CategoryTrend {
  category?: string
  label?: string
  this_month?: number
  last_month?: number
  change_pct?: number
}

export interface Discretionary {
  budget?: number
  spent?: number
  remaining?: number
  fixed_spend?: number
}

export interface AnnualizedItem {
  label?: string
  monthly?: number
  annual?: number
}

export interface FinanceInsights {
  monthly_comparison?: MonthlyComparison[]
  top_categories?: TopCategory[]
  top_merchants?: TopMerchant[]
  daily_avg?: number
  projected_total?: number
  days_elapsed?: number
  days_left?: number
  recurring?: RecurringItem[]
  pace_data?: PacePoint[]
  pace_status?: string
  pace_diff?: number
  pace_history?: PaceHistoryEntry[]
  dow_spending?: DayOfWeekSpending[]
  category_trends?: CategoryTrend[]
  eat_out_streak?: number
  budget_streak?: number
  daily_budget?: number
  discretionary?: Discretionary
  annualized?: AnnualizedItem[]
  account_limits?: Record<string, number>
  total_monthly_budget?: number
}

export interface FinanceData {
  last_updated?: string
  net_worth?: FinanceNetWorth
  accounts?: FinanceAccount[]
  monthly_spending?: MonthlySpending
  spending_trend?: SpendingTrendPoint[]
  recent_transactions?: RecentTransaction[]
  balance_history?: BalanceHistoryPoint[]
  savings_rate?: SavingsRate
  portfolios?: FinancePortfolios
  all_positions?: unknown[]
  performance?: FinancePerformance
  insights?: FinanceInsights
  targets_positions?: unknown[]
  watchlist?: unknown[]
}

// ============================================================================
// /api/regime/distributions
// ============================================================================

export interface RegimeDistStats {
  n?: number
  mean?: number
  vol?: number
  skew?: number
  kurt?: number
  var_5?: number
  var_1?: number
  cvar_5?: number
  cvar_1?: number
  fallback?: boolean
}

export interface RegimeDistributions {
  as_of?: string
  distributions?: Record<string, RegimeDistStats>
}

// ============================================================================
// /api/signals/vix_term_structure
// ============================================================================

export interface VixHistoryPoint {
  date?: string
  vix?: number
  vix3m?: number
  ratio?: number
  regime?: string
}

export interface VixTermStructure {
  as_of?: string
  vix?: number
  vix3m?: number
  ratio?: number
  regime?: string
  persistence_days?: number
  action?: string
  severity?: string
  ratio_30d_mean?: number
  ratio_30d_max?: number
  ratio_30d_min?: number
  history?: VixHistoryPoint[]
  error?: string
}

// ============================================================================
// Phase 7-9: Quant features
// ============================================================================

export interface RuinHorizon {
  days: number
  prob_ruin: number
  worst_5pct_equity: number
  median_end_equity: number
  worst_case_equity?: number
}

export interface RuinProbability {
  current_equity: number
  floor: number
  floor_pct: number
  n_paths: number
  as_of: string
  tickers?: string[]
  horizons: Record<string, RuinHorizon>
  status?: string
  source?: string
  stale?: boolean
  reason?: string
}

export interface ForecastHorizon {
  days: number
  expected_return: number
  median_return: number
  std: number
  var_5: number
  cvar_5: number
  prob_positive: number
  state_probabilities: Record<string, number>
}

export interface RegimeForecast {
  current_regime: string
  n_paths: number
  as_of: string
  horizons: Record<string, ForecastHorizon>
  source?: string
}

export interface StrategyEV {
  strategy: string
  n_trades: number
  win_rate?: number
  avg_win?: number
  avg_loss?: number
  ev_per_trade?: number
  ev_per_trade_pct?: number
  profit_factor?: number | null
  ci_low?: number
  ci_high?: number
  classification?: 'positive' | 'negative' | 'uncertain' | 'unknown'
  status?: string
}

export interface SignalEVResponse {
  strategies: StrategyEV[]
  source?: string
}

// ============================================================================
// /api/system/health/universes
// ============================================================================

export interface UniverseInfo {
  market_id?: string
  name?: string
  mode?: string          // 'active' | 'passive' | etc.
  exchange?: string
  currency?: string
  region?: string
}

// ============================================================================
// /api/pnl_filter_options — P&L slicer dropdown options
// ============================================================================

export interface PnlFilterOptions {
  markets: string[]
  strategies: string[]
  sectors: string[]
}

// Trade entry returned by /api/trades?market_id=X&strategy=Y&sector=Z
export interface PnlTrade {
  date?: string
  ticker?: string
  strategy?: string
  sector?: string
  market_id?: string
  pnl?: number
  realized_pnl?: number
  side?: 'buy' | 'sell'
}
