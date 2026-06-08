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
    paperProgress: () => [...qk.research.all(), 'paper-progress'] as const,
    // Track 3a: Variant-D dashboard endpoints
    discoveryFunnel: (days: number) => [...qk.research.all(), 'discovery-funnel', days] as const,
    queueHealth: () => [...qk.research.all(), 'queue-health'] as const,
  },
  knowledge: {
    all: () => [...qk.all, 'knowledge'] as const,
    openContradictions: (params: Record<string, unknown> = {}) =>
      [...qk.knowledge.all(), 'contradictions', 'open', params] as const,
    sources: (params: Record<string, unknown> = {}) =>
      [...qk.knowledge.all(), 'sources', params] as const,
    sourceById: (id: string) => [...qk.knowledge.all(), 'sources', id] as const,
    strategySummary: (strategy: string, params: Record<string, unknown> = {}) =>
      [...qk.knowledge.all(), 'strategy', strategy, 'summary', params] as const,
    contradictionsTimeline: (days: number) =>
      [...qk.knowledge.all(), 'contradictions-timeline', days] as const,
    digestHistory: (limit: number) =>
      [...qk.knowledge.all(), 'digest-history', limit] as const,
    extractionConfidence: () => [...qk.knowledge.all(), 'extraction-confidence'] as const,
    strategySummaries: () => [...qk.knowledge.all(), 'strategy-summaries'] as const,
  },
  promotions: {
    pending: () => ['promotions', 'pending'] as const,
  },
  forge: {
    all: () => [...qk.all, 'forge'] as const,
    state: () => [...qk.forge.all(), 'state'] as const,
  },
  pnl: {
    filterOptions: () => [...qk.all, 'pnl', 'filter-options'] as const,
    trades: (filters: Record<string, string>) => [...qk.all, 'pnl', 'trades', filters] as const,
  },
  admin: {
    all: () => [...qk.all, 'admin'] as const,
    universes: () => [...qk.admin.all(), 'universes'] as const,
    strategies: () => [...qk.admin.all(), 'strategies'] as const,
    audit: (params: Record<string, unknown> = {}) => [...qk.admin.all(), 'audit', params] as const,
  },
} as const
