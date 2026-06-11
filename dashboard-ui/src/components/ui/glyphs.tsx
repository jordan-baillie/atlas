/** Mission Control glyph set — stroke=currentColor so glyphs tint by context
 *  (and glow via CSS drop-shadow filters). 24x24 viewBox, 1.75 stroke. */

export interface GlyphProps {
  size?: number
  className?: string
}

function svgProps({ size = 16, className = '' }: GlyphProps) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    className,
    'aria-hidden': true,
  }
}

export function GlyphFlame(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="M12 3c1 3-1.5 4.5-1.5 7a3.5 3.5 0 0 0 7 .5C19.5 14 20 16 20 17a8 8 0 1 1-16 0c0-3 2-5.5 3.5-7C8 8.5 9 5.5 12 3Z" />
      <path d="M12 21a3.5 3.5 0 0 1-3.5-3.5c0-1.8 1.6-3 2.5-4.5 1.4 1.2 4.5 2.5 4.5 4.5A3.5 3.5 0 0 1 12 21Z" />
    </svg>
  )
}

export function GlyphBook(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5v14Z" />
      <path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5" />
      <path d="M9 7h7M9 11h5" />
    </svg>
  )
}

export function GlyphSignal(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none" />
      <path d="M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7" />
      <path d="M5.7 18.3a9 9 0 0 1 0-12.6M18.3 5.7a9 9 0 0 1 0 12.6" />
    </svg>
  )
}

export function GlyphCommand(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <circle cx="12" cy="12" r="7.5" />
      <circle cx="12" cy="12" r="2" />
      <path d="M12 2v3.5M12 18.5V22M2 12h3.5M18.5 12H22" />
    </svg>
  )
}

export function GlyphGate(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="M4 21V8a8 8 0 0 1 16 0v13" />
      <path d="M4 13h16M9 21v-8M15 21v-8" />
    </svg>
  )
}

export function GlyphPulse(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="M2 12h4l2.5-7 4 14 2.5-7h7" />
    </svg>
  )
}

export function GlyphShield(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="M12 2 4.5 5v6c0 5 3.2 9 7.5 11 4.3-2 7.5-6 7.5-11V5L12 2Z" />
    </svg>
  )
}

export function GlyphClock(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7v5l3.5 2" />
    </svg>
  )
}

export function GlyphFeed(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="M4 6h16M4 12h12M4 18h8" />
      <circle cx="19" cy="17.5" r="2.5" />
    </svg>
  )
}

export function GlyphCheck(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="m4.5 12.5 5 5 10-11" />
    </svg>
  )
}

export function GlyphX(p: GlyphProps) {
  return (
    <svg {...svgProps(p)}>
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  )
}
