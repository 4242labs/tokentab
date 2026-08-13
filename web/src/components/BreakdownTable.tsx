import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { BreakdownRow } from '@/lib/api'
import { short, usd } from '@/lib/format'
import { cn } from '@/lib/utils'

const LIMIT = 14

/**
 * One dimension's slice of the period. A row is a filter: clicking it narrows
 * the whole dashboard to that value, clicking the active one clears it.
 * The share bar sits behind the row rather than in its own column — the widest
 * row is the biggest spender, readable without parsing a number.
 */
export function BreakdownTable({
  rows,
  dim,
  active,
  onSelect,
}: {
  rows: BreakdownRow[] | undefined
  dim: string
  active: string | undefined
  onSelect: (dim: string, value: string) => void
}) {
  if (!rows?.length) {
    return <p className="py-6 text-center text-sm text-muted-foreground">No usage.</p>
  }
  const max = Math.max(...rows.map((r) => r.value)) || 1

  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="font-mono text-xs uppercase tracking-wider">{dim}</TableHead>
          <TableHead className="text-right font-mono text-xs uppercase tracking-wider">
            Value
          </TableHead>
          <TableHead className="text-right font-mono text-xs uppercase tracking-wider">
            Tokens
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.slice(0, LIMIT).map((r) => {
          const isActive = active === r.key
          return (
            <TableRow
              key={r.key}
              onClick={() => onSelect(dim, r.key)}
              aria-selected={isActive}
              className={cn(
                'relative cursor-pointer',
                isActive && 'bg-accent text-accent-foreground',
              )}
            >
              <TableCell className="relative max-w-0 truncate font-medium">
                {/* Share of the largest row, drawn behind the label. */}
                <span
                  aria-hidden
                  className="absolute inset-y-1 left-0 -z-10 rounded-[var(--r-2)] opacity-15"
                  style={{
                    width: `${Math.max(2, (r.value / max) * 100)}%`,
                    backgroundColor: 'var(--emerald)',
                  }}
                />
                <span className="relative">{r.key}</span>
              </TableCell>
              <TableCell className="text-right tabular-nums">{usd(r.value)}</TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                {short(r.tokens)}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}
