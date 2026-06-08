import { useEffect, useState } from 'react'
import { Badge } from '../shared/Badge'
import type { BadgeVariant } from '../shared/Badge'
import { Skeleton } from '../layout/Skeleton'
import { fmtRelativeTime } from '../../lib/format'

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

// Map classification → Badge variant
const CLASS_VARIANT: Record<string, BadgeVariant> = {
  AUTO_FIX:             'danger',
  ASSIST:               'info',
  ESCALATE:             'warning',
  ESCALATE_DEFERRED:    'warning',
  IGNORE:               'success',
  IGNORE_PENDING_CLEAR: 'success',
  UNCLASSIFIED:         'neutral',
}

// Map tier → Badge variant
const TIER_VARIANT: Record<number, BadgeVariant> = {
  0:  'danger',
  1:  'warning',
  2:  'warning',
  99: 'neutral',
}

const CLASS_LABEL: Record<string, string> = {
  AUTO_FIX:             'AUTO FIX',
  ASSIST:               'ASSIST',
  ESCALATE:             'ESCALATE',
  ESCALATE_DEFERRED:    'ESCALATE DEF',
  IGNORE:               'IGNORE',
  IGNORE_PENDING_CLEAR: 'IGNORE PEND',
  UNCLASSIFIED:         'UNCLASSIFIED',
}

function truncateStr(s: string | null, max = 70): string {
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
      <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold mb-4">
        Top Errors — Last 24h
      </div>

      {loading ? (
        <div className="space-y-2">
          {[1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} className="h-8" />
          ))}
        </div>
      ) : error ? (
        <div className="text-sm" style={{ color: 'var(--color-red)' }}>{error}</div>
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
                <th className="py-2 pr-3 text-right tabular-nums">Count</th>
                <th className="py-2 pr-3 text-left">Classification</th>
                <th className="py-2 pr-3 text-left">Tier</th>
                <th className="py-2 text-left hidden sm:table-cell">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, idx) => {
                const clsVariant = CLASS_VARIANT[row.classification] ?? 'neutral'
                const clsLabel = CLASS_LABEL[row.classification] ?? row.classification.replace(/_/g, ' ')
                const tierVariant = TIER_VARIANT[row.tier] ?? 'neutral'
                const tierLabel = row.tier === 99 ? '—' : `T${row.tier}`
                const excLabel = row.exc_type ?? row.level
                return (
                  <tr
                    key={row.fingerprint}
                    className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-alt)] transition-colors"
                  >
                    <td className="py-2 pr-3 font-mono tabular-nums text-[var(--color-text-muted)] text-xs">{idx + 1}</td>
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
                        {truncateStr(row.message, 70)}
                      </div>
                    </td>
                    <td className="py-2 pr-3 text-right font-mono tabular-nums text-xs">{row.occurrence_count.toLocaleString()}</td>
                    <td className="py-2 pr-3">
                      <Badge variant={clsVariant} size="xs">{clsLabel}</Badge>
                    </td>
                    <td className="py-2 pr-3">
                      <Badge variant={tierVariant} size="xs">{tierLabel}</Badge>
                    </td>
                    <td className="py-2 text-xs text-[var(--color-text-muted)] font-mono hidden sm:table-cell"
                        title={row.last_seen_ts}>
                      {fmtRelativeTime(row.last_seen_ts)}
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
