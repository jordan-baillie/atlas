import { useReducedMotion } from '../../hooks/useReducedMotion'

/**
 * Fixed, pointer-events-none backdrop: vignette + grid + scanlines (static)
 * plus two aurora drift blobs and a slow radar sweep (transform-only, GPU
 * composited). Blobs tint via --accent-section, so the backdrop re-tints as
 * the active tab changes; the `key` remount gives a soft crossfade.
 */
export function MissionControlBackdrop({ section }: { section: string }) {
  const reduced = useReducedMotion()
  return (
    <div className="fixed inset-0 z-0 pointer-events-none overflow-hidden" aria-hidden>
      {/* vignette */}
      <div
        className="absolute inset-0"
        style={{
          background:
            'radial-gradient(ellipse 120% 90% at 50% -10%, color-mix(in srgb, var(--accent-section, var(--color-accent)) 6%, transparent), transparent 60%)',
          transition: 'background 400ms ease',
        }}
      />
      <div className="absolute inset-0 mc-grid" />
      <div className="absolute inset-0 mc-scanlines" />
      {!reduced && (
        <div key={section} className="absolute inset-0" style={{ opacity: 1, transition: 'opacity 400ms ease' }}>
          <div
            className="mc-drift-a absolute rounded-full"
            style={{
              width: '58vw',
              height: '58vw',
              top: '-18vw',
              left: '-12vw',
              opacity: 'var(--mc-aurora-alpha)',
              background:
                'radial-gradient(closest-side, var(--accent-section, var(--color-accent)) 0%, transparent 70%)',
              willChange: 'transform',
            }}
          />
          <div
            className="mc-drift-b absolute rounded-full"
            style={{
              width: '52vw',
              height: '52vw',
              bottom: '-20vw',
              right: '-10vw',
              opacity: 'var(--mc-aurora-alpha)',
              background:
                'radial-gradient(closest-side, var(--accent-section-hot, var(--color-accent)) 0%, transparent 70%)',
              willChange: 'transform',
            }}
          />
          {/* radar pass */}
          <div
            className="mc-sweep absolute left-0 right-0 h-[2px]"
            style={{
              background:
                'linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent-section, var(--color-accent)) 30%, transparent), transparent)',
              opacity: 0.05,
            }}
          />
        </div>
      )}
    </div>
  )
}
