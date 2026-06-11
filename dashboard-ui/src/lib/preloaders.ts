/**
 * Lazy preload helpers for tab code-split chunks (breaks the App<->TabBar ESM cycle).
 */
export const preloadCommandTab = () => import('../components/command/CommandTab')
export const preloadPortfolioTab = () => import('../components/portfolio/PortfolioTab')
export const preloadForgeTab = () => import('../components/forge/ForgeTab')
export const preloadLiveTab = () => import('../components/live/LiveTab')
