import { useEffect, useState } from 'react'

interface FingerprintRow {
  fingerprint: string
  occurrence_count: number
  message: string
  service: string | null
  level: string
  classification: string
  tier: number
  file_path: string | null
  line_number: number | null
  exc_type: string | null
  first_seen_ts: string
  last_seen_ts: string
}

interface FingerprintsResponse {
  hours: number
  limit: number
  fingerprints: FingerprintRow[]
}

const CLASS_BADGE: Record<string, string> = {
  AUTO_FIX: 'bg-red-500/20 text-red-400',
  ASSIST: 'bg-blue-500/20 text-blue-400',
  ESCALATE: 'bg-orange-500/20 text-orange-400',
  ESCALATE_DEFERRED: 'bg-orange-400/20 text-orange-300',
  IGNORE: 'bg-green-500/20 text-green-400',
  IGNORE_PENDING_CLEAR: 'bg-green-400/20 text-green-300',
  UNCLASSIFIED: 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]',
}

const TIER_BADGE: Record<number, string> = {
  0: 'bg-red-500/20 text-red-400',
  1: 'bg-orange-500/20 text-orange-400',
  2: 'bg-yellow-500/20 text-yellow-400',
  99: 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]',
}

function truncate(s: string | null, max = 60): string {
  if (!s) return '—'
  return s.length > max ? s.slice(0, max) + '…' : s
}

export function TopFingerprintsTable() {
  const [rows, setRows] = useState<FingerprintRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch('/api/error_remediation/fingerprints?hours=24&limit=10')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json: FingerprintsResponse = await res.json()
        if (!cancelled) {
          setRows(json.fingerprints)
          setError(null)
        }
      } catch (e: unknown) {
        if (!cancelled) setError(String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [])

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-4">
        Top Errors — Last 24h
      </div>

      {loading ? (
        <div className="space-y-2">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="h-8 bg-[var(--color-surface-alt)] rounded animate-pulse" />
          ))}
        </div>
      ) : error ? (
        <div className="text-sm text-red-500">{error}</div>
      ) : rows.length === 0 ? (
        <div className="text-sm text-[var(--color-text-muted)]">No errors in the last 24h — all quiet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] border-b border-[var(--color-border)]">
                <th className="py-2 pr-3 text-left w-8">#</th>
                <th className="py-2 pr-3 text-left">Service</th>
                <th className="py-2 pr-3 text-left">Error / Message</th>
                <th className="py-2 pr-3 text-right">Count</th>
                <th className="py-2 pr-3 text-left">Classification</th>
                <th className="py-2 text-left">Tier</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, idx) => {
                const badgeCls = CLASS_BADGE[row.classification] ?? CLASS_BADGE.UNCLASSIFIED
                const tierCls = TIER_BADGE[row.tier] ?? TIER_BADGE[99]
                const tierLabel = row.tier === 99 ? '—' : String(row.tier)
                const excLabel = row.exc_type ?? row.level
                return (
                  <tr
                    key={row.fingerprint}
                    className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-alt)] transition-colors"
                  >
                    <td className="py-2 pr-3 font-mono text-[var(--color-text-muted)] text-xs">{idx + 1}</td>
                    <td className="py-2 pr-3 text-xs font-mono max-w-[80px] truncate" title={row.service ?? undefined}>
                      {row.service ?? '—'}
                    </td>
                    <td className="py-2 pr-3 text-xs max-w-[260px]">
                      <div className="font-mono text-[var(--color-text-muted)] text-[10px] mb-0.5">{excLabel}</div>
                      <div
                        className="truncate text-[var(--color-text)]"
                        title={row.message}
                        style={{ maxWidth: '260px' }}
                      >
                        {truncate(row.message, 70)}
                      </div>
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">{row.occurrence_count.toLocaleString()}</td>
                    <td className="py-2 pr-3">
                      <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium uppercase ${badgeCls}`}>
                        {row.classification.replace(/_/g, ' ')}
                      </span>
                    </td>
                    <td className="py-2">
                      <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-mono ${tierCls}`}>
                        {tierLabel}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
