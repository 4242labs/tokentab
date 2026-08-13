// The JSON API served by tokentab.py. Relative URLs (no leading slash) so the
// bundle works mounted at any path, matching vite's `base: './'`.

export type CashKind = 'cash' | 'allocated'

export interface Headline {
  cash_usd: number
  cash_kind: CashKind
  allocated_usd: number
  value_usd: number
  tokens: number
  events: number
}

export interface BreakdownRow {
  key: string
  value: number
  tokens: number
}

export interface PlanRow {
  key: string
  account: string | null
  fee: number
  cash: number
  allocated: number
  ended?: string | null
  inferred_end?: boolean
}

export interface DayRow {
  day: string
  value: number
  tokens: number
}

export interface Summary {
  period: { from: string; to: string; preset: string }
  headline: Headline
  notes: string[]
  filters: Record<string, string>
  breakdown: Record<string, BreakdownRow[]>
  plans: PlanRow[]
  daily: DayRow[]
  updated: string
}

export type Filters = Record<string, string | undefined>

/** Dimensions the API can both filter and break down by, in display order. */
export const DIMS = [
  'account',
  'provider',
  'billing',
  'project',
  'repo',
  'machine',
  'model',
  'source',
] as const

export type Dim = (typeof DIMS)[number]

/** Only these narrow a flat-plan fee into an allocated share; an account still
 *  pays its own plan in full, so filtering by account keeps cash as cash. */
export const ALLOCATING_DIMS: Dim[] = ['project', 'repo', 'model', 'machine', 'source']

export const PRESETS: [string, string][] = [
  ['cycle', 'This billing cycle'],
  ['prev_cycle', 'Previous cycle'],
  ['7d', 'Last 7 days'],
  ['30d', 'Last 30 days'],
  ['mtd', 'Month to date'],
  ['all', 'All time'],
]

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${path} -> ${res.status} ${res.statusText}`)
  return (await res.json()) as T
}

export const fetchFilters = () => get<Record<string, string[]>>('api/filters')

export function fetchSummary(filters: Filters): Promise<Summary> {
  const q = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v) as [string, string][],
  )
  return get<Summary>(`api/summary?${q}`)
}
