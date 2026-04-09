// Rule: rendering-content-visibility — long lists defer offscreen layout/paint
import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  label: string
  render?: (row: T) => ReactNode
  align?: 'left' | 'right' | 'center'
  className?: string
}

interface DataTableProps<T> {
  columns: Column<T>[]
  data: T[]
  emptyMessage?: string
}

export function DataTable<T>({ columns, data, emptyMessage = 'No data available' }: DataTableProps<T>) {
  if (data.length === 0) {
    return (
      <div className="text-center py-8 text-sm text-[var(--color-text-muted)]">{emptyMessage}</div>
    )
  }

  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={`text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] py-2 px-3 font-medium border-b border-[var(--color-border)] ${
                  col.align === 'right' ? 'text-right' : col.align === 'center' ? 'text-center' : 'text-left'
                } ${col.className ?? ''}`}
              >
                {col.label}
              </th>
            ))}
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
                return (
                  <td
                    key={col.key}
                    className={`py-3 px-3 text-sm ${
                      col.align === 'right' ? 'text-right' : col.align === 'center' ? 'text-center' : 'text-left'
                    } ${col.className ?? ''}`}
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
