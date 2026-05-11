const EM_DASH = '\u2014'

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  return v.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function fmtCcy(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  return '$' + Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function fmtSignedCcy(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  const sign = v >= 0 ? '+$' : '-$'
  return sign + Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  return v.toFixed(digits) + '%'
}

export function fmtSignedPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  const sign = v >= 0 ? '+' : ''
  return sign + v.toFixed(digits) + '%'
}

export function pnlClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return 'text-[var(--color-text-muted)]'
  if (v > 0) return 'text-[var(--color-green)]'
  if (v < 0) return 'text-[var(--color-red)]'
  return 'text-[var(--color-text-muted)]'
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return EM_DASH
  const s = Math.floor(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  const d = Math.floor(h / 24)
  return `${d}d ${h % 24}h`
}

export function daysHeld(entryDate: string | null | undefined): number | null {
  if (!entryDate) return null
  const entry = new Date(entryDate)
  if (Number.isNaN(entry.getTime())) return null
  const now = new Date()
  return Math.floor((now.getTime() - entry.getTime()) / (1000 * 60 * 60 * 24))
}

export function fmtDateShort(date: string | null | undefined): string {
  if (!date) return EM_DASH
  const d = new Date(date)
  if (Number.isNaN(d.getTime())) return EM_DASH
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function fmtRelativeTime(date: string | null | undefined): string {
  if (!date) return EM_DASH
  const d = new Date(date)
  if (Number.isNaN(d.getTime())) return EM_DASH
  const diffMs = Date.now() - d.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  if (diffSec < 60) return `${diffSec}s ago`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  const diffDay = Math.floor(diffHr / 24)
  return `${diffDay}d ago`
}

export function fmtAud(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  return '$' + Math.abs(v).toLocaleString('en-AU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function fmtAudSigned(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  const sign = v >= 0 ? '+$' : '-$'
  return sign + Math.abs(v).toLocaleString('en-AU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function fmtSignedNum(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return EM_DASH
  const abs = Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  // Explicit + for positives (including zero); negatives get '-' from Math.sign handling
  return (v >= 0 ? '+' : '-') + abs
}
