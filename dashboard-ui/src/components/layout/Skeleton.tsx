/**
 * Skeleton — shimmer placeholder for loading states.
 * Uses the `.skeleton` CSS class (defined in index.css) for a linear-gradient
 * sweep animation rather than the heavier Tailwind `animate-pulse` utility.
 */
export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} />
}
