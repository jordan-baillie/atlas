/**
 * Lazy preload helpers for tab code-split chunks.
 *
 * Extracted into a standalone module to break the circular ESM import between
 * App.tsx (imports TabBar) and TabBar.tsx (imports preloaders from App).
 * That cycle causes a TDZ ("Cannot access before initialization") error in
 * Chromium native-ESM mode (Playwright, strict-mode browsers).
 */

export const preloadPortfolioTab = () => import('../components/portfolio/PortfolioTab')
export const preloadFinanceTab = () => import('../components/finance/FinanceTab')
export const preloadResearchTab = () => import('../components/research/ResearchTab')
export const preloadRemediationTab = () => import('../components/error_remediation/RemediationTab')
export const preloadControlsTab = () => import('../components/controls/ControlsTab')
