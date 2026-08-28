import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { Headline } from '@/lib/api'
import { num, short, usd } from '@/lib/format'
import { cn } from '@/lib/utils'

/** The three money colours, defined once in bridge.css against DS status tokens
 *  and re-declared under `theme-dark` so each picks up its on-dark emphasis
 *  variant. Never a literal, and never `--emerald`/`--amber` read direct. */
const CASH = 'text-money-cash'
const ALLOC = 'text-money-allocated'
const VALUE = 'text-money-value'

function Figure({
  label,
  value,
  color,
  foot,
  badge,
  dashed,
  help,
}: {
  label: string
  value: string
  /** A `text-money-*` utility; omitted leaves the figure at the foreground. */
  color?: string
  foot: string
  badge?: string
  dashed?: boolean
  help: string
}) {
  return (
    <Card className={cn('gap-2 py-5', dashed && 'border-dashed')}>
      <CardHeader className="px-5">
        <CardTitle className="flex items-center gap-2 font-mono text-xs font-medium uppercase tracking-wider text-muted-foreground">
          <Tooltip>
            <TooltipTrigger className="cursor-help underline decoration-dotted underline-offset-4">
              {label}
            </TooltipTrigger>
            <TooltipContent className="max-w-72">{help}</TooltipContent>
          </Tooltip>
          {badge && (
            <Badge variant="outline" className="font-mono text-xs uppercase">
              {badge}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-5">
        <p className={cn('font-heading text-3xl font-semibold tabular-nums', color)}>{value}</p>
        <p className="mt-1 text-xs text-muted-foreground">{foot}</p>
      </CardContent>
    </Card>
  )
}

export function HeadlineCards({ h }: { h: Headline }) {
  const allocated = h.cash_kind === 'allocated'
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Figure
        label="Cash out"
        value={usd(h.cash_usd)}
        color={allocated ? ALLOC : CASH}
        dashed={allocated}
        badge={allocated ? 'allocated' : undefined}
        foot={
          allocated
            ? 'allocated share of the plan fee — not a cash figure'
            : 'real money in the period'
        }
        help="Real money that left the account: flat plan fees plus metered API charges. Narrowing by project, repo, model, machine or source switches this to an allocated share, because a flat plan has no per-call price."
      />
      <Figure
        label="Allocated cost"
        value={usd(h.allocated_usd)}
        color={ALLOC}
        dashed
        foot="flat-plan fees split by token share"
        help="Accounting, not measurement. A subscription fee split across projects in proportion to tokens used within the billing cycle. Fees are never pro-rated."
      />
      <Figure
        label="Value"
        value={usd(h.value_usd)}
        color={VALUE}
        foot="same usage at published list rates"
        help="What this usage would have cost on metered API pricing, including local-model traffic. Each event is priced at the list rate of the day it happened, and that number is kept. A later price change cannot move it."
      />
      <Figure
        label="Tokens"
        value={short(h.tokens)}
        foot={`${num(h.events)} calls`}
        help="Input, output, cache read and cache write, summed. The same basis used to allocate flat-plan fees."
      />
    </div>
  )
}
