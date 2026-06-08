import { useEffect, useState } from 'react'
import { ClassificationBreakdown } from './ClassificationBreakdown'
import { ErrorVolumeChart } from './ErrorVolumeChart'
import { TopFingerprintsTable } from './TopFingerprintsTable'
import { StatCard } from '../shared/StatCard'
import { Badge } from '../shared/Badge'
import type { BadgeVariant } from '../shared/Badge'
import { StatusDot } from '../shared/StatusDot'
import { Skeleton } from '../layout/Skeleton'

interface Summary {
  as_of: string
  errors_total: number
  errors_last_24h: number
  errors_unclassified: number
  by_classification: Record<string, number>
  by_remediation_status: Record<string, number>
  attempts_total: number
  attempts_by_status: Record<string, number>
  audit_log_total: number
  phase: number
  phase_3_enabled: boolean
  dry_run: boolean
}

interface Health {
  as_of: string
  errors_last_24h: number
  classifier_backlog: number
  classifier_backlog_ok: boolean
  audit_writes_24h: number
  halt_active: boolean
  halt_files_present: string[]
  halt_reasons: Array<{ path: string; name: string; reason: string }>
  phase: number
  phase_3_enabled: boolean
  ok: boolean
}

function statusBadge(summary: Summary, health: Health): { variant: BadgeVariant; label: string } {
  if (health.halt_active) return { variant: 'danger', label: 'HALTED' }
  if (summary.dry_run) return { variant: 'warning', label: 'DRY-RUN' }
  return { variant: 'success', label: 'ACTIVE' }
}

export function RemediationPanel() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    try {
      const [s, h] = await Promise.all([
        fetch('/api/error_remediation/summary').then(r => r.ok ? r.json() : Promise.reject(r.statusText)),
        fetch('/api/error_remediation/health').then(r => r.ok ? r.json() : Promise.reject(r.statusText)),
      ])
      setSummary(s as Summary); setHealth(h as Health); setError(null)
    } catch (e: unknown) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    const t = setInterval(() => { void load() }, 60_000)
    return () => clearInterval(t)
  }, [])

  if (loading) return (
    <div className="space-y-4">
      <Skeleton className="h-24" />
      <Skeleton className="h-48" />
    </div>
  )
  if (error) return (
    <div className="p-5 text-sm" style={{ color: 'var(--color-red)' }}>Error: {error}</div>
  )
  if (!summary || !health) return null

  const { variant: statusVariant, label: statusLabel } = statusBadge(summary, health)
  const autoFixed = summary.attempts_by_status?.['success'] ?? 0
  const fixRate = summary.attempts_total > 0
    ? ((autoFixed / summary.attempts_total) * 100).toFixed(1) + '%'
    : '—'

  return (
    <div className="space-y-4 md:space-y-6">
      {/* Top stat strip */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          label="ERRORS / 24H"
          value={summary.errors_last_24h.toLocaleString()}
          sub={`${summary.errors_total.toLocaleString()} total`}
          subColor="neutral"
        />
        <StatCard
          label="AUTO-FIXED"
          value={autoFixed.toLocaleString()}
          sub={`Fix rate: ${fixRate}`}
          subColor={autoFixed > 0 ? 'positive' : 'neutral'}
          accent={autoFixed > 0 ? 'var(--color-green)' : undefined}
        />
        <StatCard
          label="CLASSIFIER BACKLOG"
          value={health.classifier_backlog.toLocaleString()}
          sub={health.classifier_backlog_ok ? 'OK' : 'Backlogged'}
          subColor={health.classifier_backlog_ok ? 'positive' : 'negative'}
        />
        <StatCard
          label="UNCLASSIFIED"
          value={summary.errors_unclassified.toLocaleString()}
          sub={`Phase ${summary.phase}`}
          subColor="neutral"
        />
      </div>

      {/* Status banner */}
      <div className={`bg-[var(--color-surface)] border rounded-xl p-5 ${health.ok ? 'border-[var(--color-border)]' : 'border-[var(--color-red)]'}`}>
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div>
            <div className="flex items-center gap-2 mb-2"><span className="w-0.5 h-3.5 rounded-full bg-[var(--color-border)]" /><span className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold">Auto-Remediation Status</span></div>
            <div className="flex items-center gap-2 mb-1.5">
              <StatusDot
                status={health.halt_active ? 'red' : summary.dry_run ? 'amber' : 'green'}
                size="md"
                pulse={!health.halt_active && !summary.dry_run}
              />
              <span className="text-xl font-semibold">{statusLabel}</span>
              <Badge variant={statusVariant} size="sm">{statusVariant.toUpperCase()}</Badge>
            </div>
            <div className="text-xs text-[var(--color-text-muted)]">
              Phase {summary.phase}
              {summary.dry_run ? ' · capture+classify only' : ' · live'}
              {summary.phase_3_enabled ? ' · AUTO_FIX enabled' : ' · AUTO_FIX disabled'}
            </div>
          </div>
          <div className="text-right">
            <div className="text-3xl font-mono tabular-nums font-semibold">{summary.errors_last_24h}</div>
            <div className="text-xs text-[var(--color-text-muted)]">errors / 24h</div>
          </div>
        </div>
        {health.halt_active && health.halt_reasons && health.halt_reasons.length > 0 && (
          <div className="mt-4 space-y-2 border-t border-[var(--color-border)] pt-3">
            {health.halt_reasons.map((hr) => (
              <div key={hr.path} className="text-xs">
                <div className="font-mono" style={{ color: 'var(--color-red)' }}>⚠ {hr.name}</div>
                <div className="text-[var(--color-text-muted)] ml-4 mt-0.5 break-words">{hr.reason}</div>
              </div>
            ))}
          </div>
        )}
        {health.halt_active && (!health.halt_reasons || health.halt_reasons.length === 0) && (
          <div className="mt-3 text-xs font-mono" style={{ color: 'var(--color-red)' }}>
            Halt files: <code>{health.halt_files_present.join(', ')}</code>
          </div>
        )}
      </div>

      <ClassificationBreakdown summary={summary} />
      <ErrorVolumeChart />
      <TopFingerprintsTable />

      {/* Health diagnostics */}
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5">
        <div className="flex items-center gap-2 mb-3"><span className="w-0.5 h-3.5 rounded-full bg-[var(--color-border)]" /><span className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold">Health Diagnostics</span></div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">Backlog</div>
            <div className="font-mono tabular-nums" style={{ color: health.classifier_backlog_ok ? undefined : 'var(--color-red)' }}>
              {health.classifier_backlog}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">Audit writes (24h)</div>
            <div className="font-mono tabular-nums">{health.audit_writes_24h}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">Total errors</div>
            <div className="font-mono tabular-nums">{summary.errors_total.toLocaleString()}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">Fix attempts</div>
            <div className="font-mono tabular-nums">{summary.attempts_total.toLocaleString()}</div>
          </div>
        </div>
      </div>
    </div>
  )
}
