interface ChartTooltipProps {
  active?: boolean
  payload?: Array<{ value?: number; name?: string; color?: string }>
  label?: string
  formatter?: (value: number, name: string) => string
  labelFormatter?: (label: string) => string
}

/**
 * ChartTooltip — shared Recharts custom tooltip.
 * Prop signature is fixed — do not change (consumers depend on it).
 */
export function ChartTooltip({ active, payload, label, formatter, labelFormatter }: ChartTooltipProps) {
  if (!active || !payload?.length) return null
  return (
    <div
      className="dash-card !p-2.5 !shadow-lg text-xs"
      style={{ minWidth: 160, border: '1px solid var(--color-border)' }}
    >
      {/* Label row — muted, compact */}
      <div className="text-[var(--color-text-muted)] text-[10px] mb-1.5 leading-none">
        {labelFormatter ? labelFormatter(label ?? '') : label}
      </div>

      {/* Series rows — tightened line-height, 6px dots */}
      <div className="flex flex-col gap-1">
        {payload.map((entry, i) => (
          <div key={i} className="flex justify-between items-center gap-4 leading-snug">
            <span className="flex items-center gap-1.5 text-[var(--color-text-muted)]">
              {/* 6px dot — matches Badge dot sizing (was 8px w-2 h-2) */}
              <span
                className="inline-block rounded-full flex-shrink-0"
                style={{ width: 6, height: 6, background: entry.color }}
              />
              {entry.name}
            </span>
            <span className="font-mono font-medium tabular-nums text-[var(--color-text)]">
              {formatter ? formatter(entry.value ?? 0, entry.name ?? '') : entry.value?.toLocaleString()}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
