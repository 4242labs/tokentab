import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts'

import type { DayRow } from '@/lib/api'
import { short, shortDay, usd } from '@/lib/format'

function ChartTooltip({ active, payload }: { active?: boolean; payload?: { payload: DayRow }[] }) {
  if (!active || !payload?.length) return null
  const d = payload[0].payload
  return (
    <div className="rounded-[var(--r-2)] border border-border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="font-mono font-medium">{shortDay(d.day)}</p>
      <p className="mt-1 tabular-nums text-money-value">{usd(d.value)}</p>
      <p className="tabular-nums text-muted-foreground">{short(d.tokens)} tokens</p>
    </div>
  )
}

export function DailyChart({ days }: { days: DayRow[] }) {
  if (days.length === 0) {
    return <p className="py-10 text-center text-sm text-muted-foreground">No usage in this period.</p>
  }
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={days} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis
          dataKey="day"
          tickFormatter={shortDay}
          tickLine={false}
          axisLine={false}
          minTickGap={40}
          tick={{ fill: 'var(--fg-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}
        />
        <YAxis
          tickFormatter={(v: number) => `$${short(v)}`}
          tickLine={false}
          axisLine={false}
          width={52}
          tick={{ fill: 'var(--fg-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}
        />
        {/* recharts takes CSS values, not utility classes. These are the same
            theme-aware vars the `text-money-*` utilities resolve to; both are
            re-declared under body.theme-dark, so the SVG inherits the dark
            values without a second code path. */}
        <RechartsTooltip content={<ChartTooltip />} cursor={{ fill: 'var(--accent-soft)' }} />
        <Bar dataKey="value" fill="var(--money-value)" radius={2} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  )
}
