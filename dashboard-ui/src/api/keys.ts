// Centralized query key factory (stable references, type-safe)
// All keys are rooted under 'atlas' to allow targeted cache invalidation
// (e.g. queryClient.invalidateQueries({ queryKey: qk.all })).
export const qk = {
  all: ['atlas'] as const,
  portfolio: () => [...qk.all, 'portfolio'] as const,
  dashboardData: () => [...qk.portfolio(), 'dashboard-data'] as const,
  regime: {
    all: () => [...qk.all, 'regime'] as const,
    current: () => [...qk.regime.all(), 'current'] as const,
    history: (days: number) => [...qk.regime.all(), 'history', days] as const,
    transitions: () => [...qk.regime.all(), 'transitions'] as const,
    distributions: () => [...qk.regime.all(), 'distributions'] as const,
  },
  overlay: {
    all: () => [...qk.all, 'overlay'] as const,
    decisions: () => [...qk.overlay.all(), 'decisions'] as const,
  },
  system: {
    health: () => [...qk.all, 'system', 'health'] as const,
  },
  macro: {
    gauges: () => [...qk.all, 'macro', 'gauges'] as const,
  },
  positions: {
    risk: () => [...qk.all, 'positions', 'risk'] as const,
  },
  finance: () => [...qk.all, 'finance'] as const,
  signals: {
    vixTermStructure: () => [...qk.all, 'signals', 'vix-term-structure'] as const,
  },
  research: {
    all: () => [...qk.all, 'research'] as const,
    summary: () => [...qk.research.all(), 'summary'] as const,
    strategies: () => [...qk.research.all(), 'strategies'] as const,
    timeline: (days: number) => [...qk.research.all(), 'timeline', days] as const,
    experiments: (params: Record<string, unknown>) => [...qk.research.all(), 'experiments', params] as const,
    brain: () => [...qk.research.all(), 'brain'] as const,
    discoveries: () => [...qk.research.all(), 'discoveries'] as const,
    coverage: () => [...qk.research.all(), 'coverage'] as const,
  },
  promotions: {
    pending: () => ['promotions', 'pending'] as const,
  },
  pnl: {
    filterOptions: () => [...qk.all, 'pnl', 'filter-options'] as const,
    trades: (filters: Record<string, string>) => [...qk.all, 'pnl', 'trades', filters] as const,
  },
} as const
