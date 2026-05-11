/**
 * Skeleton — shimmer placeholder for loading states.
 *
 * Uses the `.skeleton` CSS class (defined in index.css) for a linear-gradient
 * sweep animation rather than the heavier Tailwind `animate-pulse` utility.
 *
 * Variants (static sub-components):
 *   <Skeleton className="h-4 w-full" />      — base usage
 *   <Skeleton.Text lines={3} />               — multi-line text block
 *   <Skeleton.Card />                         — card-shaped placeholder (h-32)
 *   <Skeleton.Chart />                        — chart placeholder (h-[280px])
 */

// ── Base ─────────────────────────────────────────────────────────────────

function SkeletonBase({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} />
}

// ── Text variant — stacked lines of decreasing width on the last line ────

function SkeletonText({ lines = 3 }: { lines?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className={`skeleton h-3 rounded ${i === lines - 1 ? 'w-2/3' : 'w-full'}`}
        />
      ))}
    </div>
  )
}

// ── Card variant — card-shaped placeholder with faint internal stripes ──

function SkeletonCard() {
  return (
    <div className="skeleton rounded-xl h-32 relative overflow-hidden">
      {/* Faint label line */}
      <div className="absolute left-4 right-4 top-4 h-2.5 bg-white/5 rounded" />
      {/* Faint value line */}
      <div className="absolute left-4 top-10 h-4 bg-white/5 rounded w-1/2" />
      {/* Faint sub line */}
      <div className="absolute left-4 top-[3.75rem] h-2 bg-white/5 rounded w-1/3" />
    </div>
  )
}

// ── Chart variant — tall placeholder with subtle bar silhouettes ─────────

function SkeletonChart() {
  // Pre-computed bar heights to avoid Math.sin on each render
  const bars = [55, 70, 48, 80, 65, 90, 72, 58, 85, 68, 76, 52]
  return (
    <div className="skeleton rounded-xl h-[280px] relative overflow-hidden">
      <div
        className="absolute inset-x-4 bottom-4 flex items-end gap-1"
        style={{ height: '65%' }}
      >
        {bars.map((h, i) => (
          <div
            key={i}
            className="flex-1 bg-white/5 rounded-t"
            style={{ height: `${h}%` }}
          />
        ))}
      </div>
    </div>
  )
}

// ── Export with sub-components attached ─────────────────────────────────

export const Skeleton = Object.assign(SkeletonBase, {
  Text: SkeletonText,
  Card: SkeletonCard,
  Chart: SkeletonChart,
})
