// Centralized query key factory (stable references, type-safe).
// Rooted under 'atlas' for targeted cache invalidation.
export const qk = {
  all: ['atlas'] as const,
  portfolio: () => [...qk.all, 'portfolio'] as const,
  dashboardData: () => [...qk.portfolio(), 'dashboard-data'] as const,
  system: {
    health: () => [...qk.all, 'system', 'health'] as const,
  },
  forge: {
    all: () => [...qk.all, 'forge'] as const,
    state: () => [...qk.forge.all(), 'state'] as const,
    map: () => [...qk.forge.all(), 'map'] as const,
  },
} as const
