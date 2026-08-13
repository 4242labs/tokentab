import { Badge } from '@/components/ui/badge'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { PlanRow } from '@/lib/api'
import { usd } from '@/lib/format'
import { cn } from '@/lib/utils'

/**
 * One row per subscription — a plan family paired with an account. The pairs
 * come from the data, so a new login appears here and a cancelled one stops
 * charging without a config change.
 */
export function PlansTable({
  rows,
  activeAccount,
  onSelect,
}: {
  rows: PlanRow[]
  activeAccount: string | undefined
  onSelect: (account: string) => void
}) {
  if (!rows.length) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        No flat plans active in this period.
      </p>
    )
  }
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="font-mono text-xs uppercase tracking-wider">Subscription</TableHead>
          <TableHead className="text-right font-mono text-xs uppercase tracking-wider">Fee</TableHead>
          <TableHead className="text-right font-mono text-xs uppercase tracking-wider">Cash</TableHead>
          <TableHead className="text-right font-mono text-xs uppercase tracking-wider">
            Allocated
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r) => {
          const isActive = !!r.account && activeAccount === r.account
          return (
            <TableRow
              key={r.key}
              onClick={() => r.account && onSelect(r.account)}
              aria-selected={isActive}
              className={cn(
                r.account && 'cursor-pointer',
                isActive && 'bg-accent text-accent-foreground',
              )}
            >
              <TableCell className="font-medium">
                <span className="flex flex-wrap items-center gap-2">
                  {r.key}
                  {r.ended && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Badge variant="outline" className="font-mono text-xs font-normal">
                          ended {r.ended}
                          {r.inferred_end ? '?' : ''}
                        </Badge>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-72">
                        {r.inferred_end
                          ? 'Cancelled on an unknown date. This end is inferred from the account’s last recorded usage, not from an invoice — hence the question mark.'
                          : 'Cancellation date taken from configuration.'}
                      </TooltipContent>
                    </Tooltip>
                  )}
                </span>
              </TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                {usd(r.fee)}
              </TableCell>
              {/* --accent-solid, not --accent: bridge.css shadows the latter with
                  the shadcn soft-highlight background. See HeadlineCards. */}
              <TableCell className="text-right tabular-nums" style={{ color: 'var(--accent-solid)' }}>
                {usd(r.cash)}
              </TableCell>
              <TableCell className="text-right tabular-nums" style={{ color: 'var(--amber)' }}>
                {usd(r.allocated)}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}
