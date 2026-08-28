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

export interface Machine {
  host: string
  /** Last time this machine reached the store — null before it ever has. */
  last_seen: string | null
  /** Last usage it reported. A machine can be healthy and have none. */
  last_event: string | null
  events: number
  state: 'ok' | 'late' | 'unknown'
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
  if (!res.ok) {
    // The server answers an error as JSON `{error}`, and that sentence names
    // the command that fixes it — a store that was migrated but never priced
    // says so. Throwing the status code instead threw the answer away.
    const body = (await res.json().catch(() => null)) as { error?: string } | null
    throw new Error(body?.error ?? `${path} -> ${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

// ---------------------------------------------------------------- demo mode
//
// `vite build --mode demo` produces a bundle with no server behind it: every
// view is precomputed by tools/make-demo.py, which runs the *real* Python
// engine over a synthetic store. There is no second implementation of the
// pricing or allocation rules here — only a lookup. The branch is constant at
// build time, so a production build drops it and never ships the fixture.

/** True in the public demo build. Nothing here is real data. */
export const DEMO = import.meta.env.MODE === 'demo'

interface DemoFixture {
  generated: string
  filters: Record<string, string[]>
  summaries: Record<string, Summary>
  machines: Machine[]
}

let fixture: Promise<DemoFixture> | null = null

function loadFixture(): Promise<DemoFixture> {
  // Fetched, not imported: ~1 MB of JSON stays out of the JS bundle, and the
  // file only exists in a demo build (public/demo.json, written by
  // tools/make-demo.py), so a production build cannot ship it by accident.
  fixture ??= get<DemoFixture>('demo.json')
  return fixture
}

/** Fixture key. Mirrors key() in tools/make-demo.py — keep the two together. */
const demoKey = (preset: string, dim?: string, value?: string) =>
  dim ? `${preset}|${dim}=${value}` : `${preset}|`

/** The demo precomputes each preset alone and with one filter; two at once
 *  would square the matrix. Extra filters are dropped, and said so. */
async function demoSummary(filters: Filters): Promise<Summary> {
  const f = await loadFixture()
  const preset = filters.preset || 'cycle'
  const active = DIMS.filter((d) => filters[d]).map((d) => [d, filters[d] as string])
  const [first, ...rest] = active
  const hit = f.summaries[first ? demoKey(preset, first[0], first[1]) : demoKey(preset)]
  const base = hit ?? f.summaries[demoKey(preset)]
  const notes = [...base.notes]
  if (rest.length)
    notes.unshift(
      `Static demo: only one filter at a time is precomputed, so ${rest
        .map(([d, v]) => `${d} = ${v}`)
        .join(', ')} ${rest.length > 1 ? 'were' : 'was'} ignored. The real dashboard ` +
        'queries the store and combines them freely.',
    )
  return { ...base, notes }
}

export const fetchMachines = () =>
  DEMO ? loadFixture().then((f) => f.machines) : get<Machine[]>('api/machines')

export const fetchFilters = () =>
  DEMO ? loadFixture().then((f) => f.filters) : get<Record<string, string[]>>('api/filters')

export function fetchSummary(filters: Filters): Promise<Summary> {
  if (DEMO) return demoSummary(filters)
  const q = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v) as [string, string][],
  )
  return get<Summary>(`api/summary?${q}`)
}
