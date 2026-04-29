import { useEffect, useState } from 'react'
import { ClassificationBreakdown } from './ClassificationBreakdown'
import { ErrorVolumeChart } from './ErrorVolumeChart'
import { TopFingerprintsTable } from './TopFingerprintsTable'

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
  phase: number
  phase_3_enabled: boolean
  ok: boolean
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
    const t = setInterval(() => { void load() }, 60_000)  // refresh every minute
    return () => clearInterval(t)
  }, [])

  if (loading) return <div className="p-5 text-sm">Loading remediation status…</div>
  if (error) return <div className="p-5 text-sm text-red-500">Error: {error}</div>
  if (!summary || !health) return null

  return (
    <div className="space-y-4 md:space-y-6">
      {/* Status banner */}
      <div className={`bg-[var(--color-surface)] border rounded-xl p-5 ${health.ok ? 'border-[var(--color-border)]' : 'border-red-500'}`}>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xs text-[var(--color-text-muted)] uppercase tracking-wider mb-1">Auto-Remediation</div>
            <div className="text-2xl font-semibold">
              {health.halt_active ? '⛔ HALTED' : (summary.dry_run ? '🟡 DRY-RUN' : '🟢 ACTIVE')}
            </div>
            <div className="text-xs text-[var(--color-text-muted)] mt-1">
              Phase {summary.phase} • {summary.dry_run ? 'capture+classify only' : 'live'}
              {summary.phase_3_enabled ? ' • AUTO_FIX enabled' : ' • AUTO_FIX disabled'}
            </div>
          </div>
          <div className="text-right">
            <div className="text-3xl font-mono">{summary.errors_last_24h}</div>
            <div className="text-xs text-[var(--color-text-muted)]">errors / 24h</div>
          </div>
        </div>
        {health.halt_active && (
          <div className="mt-3 text-xs text-red-400">
            Halt files: <code>{health.halt_files_present.join(', ')}</code>
          </div>
        )}
      </div>

      <ClassificationBreakdown summary={summary} />
      <ErrorVolumeChart />
      <TopFingerprintsTable />

      {/* Health diagnostics */}
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5">
        <div className="text-xs uppercase tracking-wider text-[var(--color-text-muted)] mb-3">Health</div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
          <div>
            <div className="text-xs text-[var(--color-text-muted)]">Backlog</div>
            <div className={`font-mono ${health.classifier_backlog_ok ? '' : 'text-red-500'}`}>{health.classifier_backlog}</div>
          </div>
          <div>
            <div className="text-xs text-[var(--color-text-muted)]">Audit writes (24h)</div>
            <div className="font-mono">{health.audit_writes_24h}</div>
          </div>
          <div>
            <div className="text-xs text-[var(--color-text-muted)]">Total errors</div>
            <div className="font-mono">{summary.errors_total}</div>
          </div>
          <div>
            <div className="text-xs text-[var(--color-text-muted)]">Fix attempts</div>
            <div className="font-mono">{summary.attempts_total}</div>
          </div>
        </div>
      </div>
    </div>
  )
}
