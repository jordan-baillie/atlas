// Rule: rendering-content-visibility — long lists defer offscreen layout/paint
import type { ReactNode } from 'react'
import { EmptyState } from './EmptyState'

export interface Column<T> {
  key: string
  label: string
  render?: (row: T) => ReactNode
  align?: 'left' | 'right' | 'center'
  className?: string
  /** When true, the header becomes a clickable sort button. Caller owns sort state. */
  sortable?: boolean
  /** Current sort direction for this column. null/undefined = unsorted. */
  sortDirection?: 'asc' | 'desc' | null
}

interface DataTableProps<T> {
  columns: Column<T>[]
  data: T[]
  emptyMessage?: string
  /** Called when a sortable column header is clicked. Receives the column key. */
  onSort?: (key: string) => void
  /**
   * Row density.
   * - 'comfortable' (default): py-3 px-3 text-sm — standard reading density
   * - 'compact': py-1.5 px-2.5 text-xs — more rows per screen
   */
  density?: 'compact' | 'comfortable'
}

function SortIndicator({ direction }: { direction?: 'asc' | 'desc' | null }) {
  if (direction === 'asc')
    return <span className="ml-1 text-[var(--color-text-muted)]" aria-hidden="true">▲</span>
  if (direction === 'desc')
    return <span className="ml-1 text-[var(--color-text-muted)]" aria-hidden="true">▼</span>
  // Unsorted: small muted bidirectional arrow
  return <span className="ml-1 text-[var(--color-text-muted)]/40 text-[8px]" aria-hidden="true">↕</span>
}

export function DataTable<T>({
  columns,
  data,
  emptyMessage = 'No data available',
  onSort,
  density = 'comfortable',
}: DataTableProps<T>) {
  if (data.length === 0) {
    return <EmptyState message={emptyMessage} />
  }

  const cellPad = density === 'compact' ? 'py-1.5 px-2.5' : 'py-3 px-3'
  const cellText = density === 'compact' ? 'text-xs' : 'text-sm'

  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full">
        {/* Sticky header — no-op without scroll container, safe to always apply */}
        <thead className="sticky top-0 z-10 bg-[var(--color-surface)]">
          <tr>
            {columns.map((col) => {
              const alignCls =
                col.align === 'right'
                  ? 'text-right'
                  : col.align === 'center'
                  ? 'text-center'
                  : 'text-left'
              const baseCls = `text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] py-2 px-3 font-medium border-b border-[var(--color-border)] ${alignCls} ${col.className ?? ''}`

              if (col.sortable && onSort) {
                return (
                  <th key={col.key} className={baseCls}>
                    <button
                      type="button"
                      onClick={() => onSort(col.key)}
                      className="inline-flex items-center gap-0.5 hover:text-[var(--color-text)] transition-colors"
                    >
                      {col.label}
                      <SortIndicator direction={col.sortDirection} />
                    </button>
                  </th>
                )
              }
              return (
                <th key={col.key} className={baseCls}>
                  {col.label}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => (
            <tr
              key={i}
              className="border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface-alt)]/50 transition-colors cv-auto-sm"
            >
              {columns.map((col) => {
                const content = col.render
                  ? col.render(row)
                  : ((row as Record<string, unknown>)[col.key] as ReactNode)
                const isRight = col.align === 'right'
                const alignCls = isRight
                  ? 'text-right'
                  : col.align === 'center'
                  ? 'text-center'
                  : 'text-left'
                // Right-aligned cells: tabular-nums + font-mono for numeric readability
                const numericCls = isRight ? 'font-mono tabular-nums' : ''
                return (
                  <td
                    key={col.key}
                    className={`${cellPad} ${cellText} ${alignCls} ${numericCls} ${col.className ?? ''}`}
                  >
                    {content as ReactNode}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
