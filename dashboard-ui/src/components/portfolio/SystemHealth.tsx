import type { SystemHealth as SystemHealthData, HealthCronJob, HealthDataFreshness } from '../../api/types'
import { EmptyState } from '../shared/EmptyState'
import { Badge } from '../shared/Badge'
import { StatusDot } from '../shared/StatusDot'
import { fmtRelativeTime } from '../../lib/format'
import type { BadgeVariant } from '../shared/Badge'

interface Props { data: SystemHealthData }

// ── Helpers ────────────────────────────────────────────────────────────────

function overallVariant(overall?: string): BadgeVariant {
  const s = (overall ?? '').toLowerCase()
  if (s === 'healthy' || s === 'ok') return 'success'
  if (s === 'degraded') return 'warning'
  if (s === 'down' || s === 'error') return 'danger'
  return 'neutral'
}

function serviceStatus(status?: string): { dot: 'green' | 'amber' | 'red'; pulse: boolean } {
  const s = (status ?? '').toLowerCase()
  if (s === 'active' || s === 'ok' || s === 'healthy' || s === 'running' || s === 'oneshot-success') {
    return { dot: 'green', pulse: true }
  }
  if (s === 'degraded' || s === 'warning') return { dot: 'amber', pulse: false }
  return { dot: 'red', pulse: false }
}

function cronVariant(j: HealthCronJob): BadgeVariant {
  if (j.exit_code != null && j.exit_code !== 0) return 'danger'
  const s = (j.status ?? '').toLowerCase()
  if (s === 'ok' || s === 'success' || s === 'completed') return 'success'
  return 'neutral'
}

function cronLabel(j: HealthCronJob): string {
  const failed = j.exit_code != null && j.exit_code !== 0
  return (j.status ?? '').toUpperCase() || (failed ? 'FAIL' : 'OK')
}

// ── Sub-components ─────────────────────────────────────────────────────────

function ServicesList({ services }: { services: Record<string, string> }) {
  const entries = Object.entries(services)
  if (entries.length === 0) return <EmptyState message="No data" className="py-4" />
  return (
    <div className="space-y-1">
      {entries.map(([name, status]) => {
        const { dot, pulse } = serviceStatus(status)
        return (
          <div
            key={name}
            className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 hover:bg-[var(--color-surface-alt)]/50 transition-colors"
          >
            <StatusDot status={dot} size="sm" pulse={pulse} />
            <div className="min-w-0 flex-1">
              <span className="text-sm">{name}</span>
            </div>
            <Badge variant={dot === 'green' ? 'success' : dot === 'amber' ? 'warning' : 'danger'} size="xs">
              {status.toUpperCase()}
            </Badge>
          </div>
        )
      })}
    </div>
  )
}

function CronJobsList({ jobs }: { jobs: Record<string, HealthCronJob> }) {
  const entries = Object.entries(jobs)
  if (entries.length === 0) return <EmptyState message="No data" className="py-4" />
  return (
    <div className="space-y-2">
      {entries.map(([name, j]) => (
        <div key={name} className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="text-sm truncate">{name}</div>
            <div className="text-xs text-[var(--color-text-muted)] font-mono tabular-nums">
              {fmtRelativeTime(j.last_run)}
            </div>
          </div>
          <Badge variant={cronVariant(j)} size="xs">
            {cronLabel(j)}
          </Badge>
        </div>
      ))}
    </div>
  )
}

function DataFreshnessList({ data }: { data: HealthDataFreshness }) {
  const items: { label: string; value: string }[] = []
  if (data.ohlcv_last_date) items.push({ label: 'OHLCV last', value: data.ohlcv_last_date })
  if (data.equity_last_date) items.push({ label: 'Equity last', value: data.equity_last_date })
  if (data.overlay_decisions_count != null)
    items.push({ label: 'Overlay decisions', value: String(data.overlay_decisions_count) })
  if (items.length === 0) return <EmptyState message="No data" className="py-4" />
  return (
    <div className="space-y-2">
      {items.map((item) => (
        <div key={item.label} className="flex items-center justify-between gap-2">
          <div className="text-sm truncate">{item.label}</div>
          <span className="text-xs font-mono tabular-nums shrink-0 text-[var(--color-text-muted)]">
            {item.value}
          </span>
        </div>
      ))}
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────

export function SystemHealth({ data }: Props) {
  const services = data.services ?? {}
  const cronJobs = data.cron ?? {}
  const freshness = data.data_freshness ?? {}

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
          SYSTEM HEALTH
        </div>
        <div className="flex items-center gap-2">
          {data.timestamp && (
            <span className="text-[10px] text-[var(--color-text-muted)] font-mono tabular-nums">
              {fmtRelativeTime(data.timestamp)}
            </span>
          )}
          <Badge variant={overallVariant(data.overall)} size="xs">
            {(data.overall ?? '').toUpperCase() || '\u2014'}
          </Badge>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3">
          <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2 font-semibold">
            SERVICES
          </div>
          <ServicesList services={services} />
        </div>

        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3">
          <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2 font-semibold">
            CRON JOBS
          </div>
          <CronJobsList jobs={cronJobs} />
        </div>

        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3">
          <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2 font-semibold">
            DATA FRESHNESS
          </div>
          <DataFreshnessList data={freshness} />
        </div>
      </div>
    </div>
  )
}
