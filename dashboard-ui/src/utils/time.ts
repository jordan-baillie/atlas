/**
 * humanRelative -- converts an ISO timestamp to a human-readable relative time string.
 *
 * Examples: "5s", "3m", "1h", "2d"
 *
 * Used by AsOfBadge and other components that need concise age display.
 */
export function humanRelative(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime()
  const secs = Math.floor(Math.abs(diffMs) / 1_000)
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h`
  return `${Math.floor(hrs / 24)}d`
}
