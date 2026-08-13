import { useEffect, useState } from 'react'

import { cn } from '@/lib/utils'

type Theme = 'light' | 'dark'

const STORAGE_KEY = 'tokentab-theme'

function readInitialTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/**
 * A 2-up segmented control (LIGHT / DARK). Toggles `body.theme-dark`, which is
 * what the design tokens and the shadcn bridge key off — the whole palette
 * flips at runtime, no rebuild.
 */
export function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>(() =>
    typeof window === 'undefined' ? 'light' : readInitialTheme(),
  )

  useEffect(() => {
    document.body.classList.toggle('theme-dark', theme === 'dark')
    localStorage.setItem(STORAGE_KEY, theme)
  }, [theme])

  return (
    <div
      className="grid grid-cols-2 gap-1 rounded-[var(--r-2)] border border-border bg-background p-1"
      role="group"
      aria-label="Theme"
    >
      {(['light', 'dark'] as const).map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => setTheme(t)}
          aria-pressed={theme === t}
          className={cn(
            'cursor-pointer rounded-[var(--r-2)] border-0 px-2 py-0.5 font-mono text-xs font-medium uppercase tracking-wider transition-colors',
            theme === t
              ? 'bg-card text-foreground shadow-sm'
              : 'bg-transparent text-muted-foreground hover:text-foreground',
          )}
        >
          {t}
        </button>
      ))}
    </div>
  )
}
