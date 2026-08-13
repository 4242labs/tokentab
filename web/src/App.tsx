import { useCallback, useEffect, useState } from 'react'
import { Info, TriangleAlert } from 'lucide-react'

import { BrandMark } from '@/components/brand-mark'
import { BreakdownTable } from '@/components/BreakdownTable'
import { DailyChart } from '@/components/DailyChart'
import { FilterBar } from '@/components/FilterBar'
import { HeadlineCards } from '@/components/HeadlineCards'
import { PlansTable } from '@/components/PlansTable'
import { ThemeSwitch } from '@/components/ThemeSwitch'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { TooltipProvider } from '@/components/ui/tooltip'
import {
  fetchFilters,
  fetchSummary,
  type Filters,
  type Summary,
} from '@/lib/api'
import { shortDay } from '@/lib/format'

/** Breakdown dimensions given their own panel, in reading order. */
const PANELS: [string, string][] = [
  ['project', 'By project'],
  ['account', 'By account'],
  ['model', 'By model'],
  ['provider', 'By provider'],
  ['machine', 'By machine'],
  ['repo', 'By repo'],
]

function Panel({
  title,
  children,
  className,
}: {
  title: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="font-mono text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

export default function App() {
  const [filters, setFilters] = useState<Filters>({ preset: 'cycle' })
  const [options, setOptions] = useState<Record<string, string[]>>({})
  const [data, setData] = useState<Summary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchFilters().then(setOptions).catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    let stale = false
    fetchSummary(filters)
      .then((d) => {
        if (!stale) {
          setData(d)
          setError(null)
        }
      })
      .catch((e: Error) => !stale && setError(e.message))
    return () => {
      stale = true
    }
  }, [filters])

  const setFilter = useCallback((key: string, value: string | undefined) => {
    setFilters((f) => ({ ...f, [key]: value }))
  }, [])

  /** Clicking a row toggles that value: select it, or clear it if already on. */
  const toggleFilter = useCallback((dim: string, value: string) => {
    setFilters((f) => ({ ...f, [dim]: f[dim] === value ? undefined : value }))
  }, [])

  const reset = useCallback(() => setFilters({ preset: filters.preset }), [filters.preset])

  return (
    <TooltipProvider delayDuration={200}>
      <div className="min-h-svh bg-background text-foreground">
        <header className="sticky top-0 z-10 border-b border-border bg-background/95 backdrop-blur">
          <div className="mx-auto flex max-w-[var(--w-xl)] flex-wrap items-center gap-x-4 gap-y-2 px-[var(--pad-x)] py-4">
            <BrandMark className="size-5" />
            <h1 className="font-heading text-lg font-semibold tracking-tight">tokentab</h1>
            {data && (
              <span className="font-mono text-xs text-muted-foreground">
                {shortDay(data.period.from)} – {shortDay(data.period.to)}
              </span>
            )}
            <span className="flex-1" />
            {data && (
              <span className="font-mono text-xs text-muted-foreground">
                updated {data.updated}
              </span>
            )}
            <ThemeSwitch />
          </div>
        </header>

        <main className="mx-auto flex max-w-[var(--w-xl)] flex-col gap-6 px-[var(--pad-x)] py-6">
          <FilterBar
            filters={filters}
            options={options}
            onChange={setFilter}
            onReset={reset}
          />

          {error && (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {!data ? (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-32 rounded-xl" />
              ))}
            </div>
          ) : (
            <>
              <HeadlineCards h={data.headline} />

              {data.notes.length > 0 && (
                <div className="flex flex-col gap-2">
                  {data.notes.map((n) => (
                    <Alert key={n}>
                      <Info />
                      <AlertDescription>{n}</AlertDescription>
                    </Alert>
                  ))}
                </div>
              )}

              <Panel title="Daily value">
                <DailyChart days={data.daily} />
              </Panel>

              <Panel title="By subscription">
                <PlansTable
                  rows={data.plans}
                  activeAccount={filters.account}
                  onSelect={(a) => toggleFilter('account', a)}
                />
              </Panel>

              {/* items-start: a short panel should not stretch to the height of
                  whatever tall one shares its row. */}
              <div className="grid items-start gap-4 lg:grid-cols-2">
                {PANELS.map(([dim, title]) => (
                  <Panel key={dim} title={title}>
                    <BreakdownTable
                      rows={data.breakdown[dim]}
                      dim={dim}
                      active={filters[dim]}
                      onSelect={toggleFilter}
                    />
                  </Panel>
                ))}
              </div>
            </>
          )}

          <p className="pb-4 text-xs text-muted-foreground">
            Flat-plan figures per project are allocated by token share, never measured. Value prices
            the same usage at published API list rates.
          </p>
        </main>
      </div>
    </TooltipProvider>
  )
}
