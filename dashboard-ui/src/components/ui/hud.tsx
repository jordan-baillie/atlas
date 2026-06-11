/** Mission Control HUD primitives. Every component reads `--accent-section`
 *  (set by a [data-section] ancestor) — no hex literals, so panels tint by
 *  context and light mode attenuates via the --mc-*-alpha knobs. */
import type { CSSProperties, ReactNode } from 'react'

// ── CornerBrackets ───────────────────────────────────────────────────────────

/** HUD corner furniture. Render inside any `relative` parent. */
export function CornerBrackets({ size = 10 }: { size?: number }) {
  const base: CSSProperties = {
    position: 'absolute',
    width: size,
    height: size,
    color: 'var(--accent-section, var(--color-accent))',
    opacity: 0.6,
    pointerEvents: 'none',
  }
  const edge = '2px solid currentColor'
  return (
    <>
      <span style={{ ...base, top: -1, left: -1, borderTop: edge, borderLeft: edge }} />
      <span style={{ ...base, top: -1, right: -1, borderTop: edge, borderRight: edge }} />
      <span style={{ ...base, bottom: -1, left: -1, borderBottom: edge, borderLeft: edge }} />
      <span style={{ ...base, bottom: -1, right: -1, borderBottom: edge, borderRight: edge }} />
    </>
  )
}

// ── HudPanel ─────────────────────────────────────────────────────────────────

interface HudPanelProps {
  title?: ReactNode
  right?: ReactNode
  /** Static glow halo (pre-rendered ::after). */
  glow?: boolean
  /** Pulse the glow opacity (e.g. while a system is running). */
  glowPulse?: boolean
  brackets?: boolean
  className?: string
  bodyClassName?: string
  children: ReactNode
}

/** The Mission Control card: accent-tinted frame + optional glow/brackets. */
export function HudPanel({
  title,
  right,
  glow = false,
  glowPulse = false,
  brackets = false,
  className = '',
  bodyClassName = '',
  children,
}: HudPanelProps) {
  const glowClass = glow || glowPulse ? `mc-glow-after ${glowPulse ? 'mc-glow-pulse' : ''}` : ''
  return (
    <section className={`mc-frame ${glowClass} p-4 ${className}`}>
      {brackets && <CornerBrackets />}
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 mb-3">
          {title && (
            <h2 className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--color-text-muted)]">
              <span
                aria-hidden
                className="inline-block w-[3px] h-3.5 rounded-full"
                style={{ background: 'var(--accent-section, var(--color-accent))' }}
              />
              {title}
            </h2>
          )}
          {right && <div className="flex items-center gap-2">{right}</div>}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  )
}

// ── Beacon ───────────────────────────────────────────────────────────────────

interface BeaconProps {
  /** CSS color (token var or named). */
  color?: string
  /** Animate the expanding ring; off = static dot + halo. */
  on?: boolean
  size?: number
  className?: string
}

/** Status beacon: solid dot + expanding ring (transform/opacity only). */
export function Beacon({ color = 'var(--accent-section, var(--color-accent))', on = true, size = 8, className = '' }: BeaconProps) {
  return (
    <span
      className={`relative inline-flex items-center justify-center shrink-0 ${className}`}
      style={{ width: size * 2, height: size * 2 }}
      aria-hidden
    >
      {on && (
        <span
          className="mc-beacon-ring absolute inset-0 rounded-full"
          style={{ border: `1.5px solid ${color}` }}
        />
      )}
      <span
        className="rounded-full"
        style={{
          width: size,
          height: size,
          background: color,
          boxShadow: `0 0 ${size}px color-mix(in srgb, ${color} 55%, transparent)`,
        }}
      />
    </span>
  )
}

// ── StreamDivider ────────────────────────────────────────────────────────────

/** Animated data-stream separator: gradient hairline + traveling pulse. */
export function StreamDivider({ className = '' }: { className?: string }) {
  return (
    <div className={`relative h-px my-1 overflow-visible ${className}`} aria-hidden>
      <div
        className="absolute inset-0"
        style={{
          background:
            'linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent-section, var(--color-accent)) 40%, transparent), transparent)',
        }}
      />
      <span
        className="mc-travel absolute top-[-2.5px] w-1.5 h-1.5 rounded-full"
        style={{
          background: 'var(--accent-section-hot, var(--color-accent))',
          boxShadow: '0 0 8px var(--accent-section-hot, var(--color-accent))',
        }}
      />
    </div>
  )
}

// ── GaugeBar ─────────────────────────────────────────────────────────────────

interface GaugeBarProps {
  /** Measured value (e.g. median slippage bps). */
  value: number | null | undefined
  /** The gate bar / threshold. */
  bar: number
  /** Render scale max; defaults to 2x the bar. */
  max?: number
  pass?: boolean | null
  /** Format for the tick label. */
  fmt?: (v: number) => string
  className?: string
}

/** Measured-value-vs-threshold meter with a tick at the bar. */
export function GaugeBar({ value, bar, max, pass, fmt = (v) => String(v), className = '' }: GaugeBarProps) {
  const scaleMax = max ?? Math.max(bar * 2, value ?? 0) * 1.05
  const pct = value == null ? 0 : Math.min(100, Math.max(0, (value / scaleMax) * 100))
  const barPct = Math.min(100, (bar / scaleMax) * 100)
  const fill =
    pass === false ? 'var(--color-negative)' : pass === true ? 'var(--color-positive)' : 'var(--color-warning)'
  return (
    <div className={className}>
      <div className="relative h-2 rounded-full bg-[var(--color-surface-alt)] overflow-visible">
        {value != null && (
          <div
            className="absolute inset-y-0 left-0 rounded-full transition-[width] duration-700"
            style={{ width: `${pct}%`, background: fill, boxShadow: `0 0 8px color-mix(in srgb, ${fill} 50%, transparent)` }}
          />
        )}
        {/* the bar tick */}
        <div
          className="absolute top-[-3px] bottom-[-3px] w-[2px] rounded"
          style={{ left: `${barPct}%`, background: 'var(--color-text-muted)' }}
          title={`bar: ${fmt(bar)}`}
        />
      </div>
      <div className="flex justify-between mt-1 text-[10px] font-mono text-[var(--color-text-muted)]">
        <span>{value == null ? '—' : fmt(value)}</span>
        <span>bar {fmt(bar)}</span>
      </div>
    </div>
  )
}

// ── GateStatusPill ───────────────────────────────────────────────────────────

interface GateStatusPillProps {
  label: string
  pass: boolean | null | undefined
  detail?: string
  className?: string
}

/** PASS / FAIL / ACCRUING pill with a beacon. */
export function GateStatusPill({ label, pass, detail, className = '' }: GateStatusPillProps) {
  const state = pass === true ? 'PASS' : pass === false ? 'FAIL' : 'ACCRUING'
  const color =
    pass === true ? 'var(--color-positive)' : pass === false ? 'var(--color-negative)' : 'var(--color-warning)'
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[10px] font-semibold tracking-[0.08em] font-mono ${className}`}
      style={{
        color,
        borderColor: `color-mix(in srgb, ${color} 40%, var(--color-border))`,
        background: `color-mix(in srgb, ${color} 8%, transparent)`,
      }}
      title={detail}
    >
      <Beacon color={color} on={pass !== true} size={4} />
      {label} {state}
    </span>
  )
}
