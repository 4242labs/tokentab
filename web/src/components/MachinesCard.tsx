import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { Machine } from '@/lib/api'
import { cn } from '@/lib/utils'

/** "3 hours ago", down to the minute — the number matters, the seconds do not. */
function ago(iso: string | null): string {
  if (!iso) return 'never'
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

const DOT: Record<Machine['state'], string> = {
  ok: 'bg-emerald-500',
  late: 'bg-red-500',
  unknown: 'bg-muted-foreground/40',
}

const WHY: Record<Machine['state'], string> = {
  ok: 'Reported in within the last couple of hours.',
  late: 'No contact for over two hours — the collector is not getting through.',
  unknown: 'Has sent usage but never announced itself. It is running a build older than heartbeats.',
}

/**
 * One row per machine: is it still reporting, and when did it last do any work.
 *
 * The dot is contact, never usage. A laptop that was shut all weekend has no
 * events and is perfectly healthy; a collector whose push has been failing since
 * Tuesday has no events either. Colouring on usage would call the first one
 * broken and the second one fine.
 */
export function MachinesCard({ rows }: { rows: Machine[] }) {
  if (!rows.length) return null
  return (
    <ul className="flex flex-col gap-2">
      {rows.map((m) => (
        <li key={m.host} className="flex items-baseline gap-2 text-sm">
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className={cn('size-2 shrink-0 translate-y-[-1px] rounded-full', DOT[m.state])}
                aria-label={m.state}
              />
            </TooltipTrigger>
            <TooltipContent>{WHY[m.state]}</TooltipContent>
          </Tooltip>
          <span className="font-mono">{m.host}</span>
          <span className="flex-1" />
          <span className="font-mono text-xs text-muted-foreground">
            seen {ago(m.last_seen)}
          </span>
          <span className="w-28 text-right font-mono text-xs text-muted-foreground">
            usage {ago(m.last_event)}
          </span>
        </li>
      ))}
    </ul>
  )
}
