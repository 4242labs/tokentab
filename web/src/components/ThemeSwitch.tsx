import { useEffect, useState } from 'react'

import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

type Theme = 'light' | 'dark'

const STORAGE_KEY = 'tokentab-theme'

function readInitialTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** Canonical @42labs/theme-switch contract. */
export function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>(() =>
    typeof window === 'undefined' ? 'light' : readInitialTheme(),
  )

  useEffect(() => {
    document.body.classList.toggle('theme-dark', theme === 'dark')
    localStorage.setItem(STORAGE_KEY, theme)
  }, [theme])

  return (
    <ToggleGroup
      type="single"
      spacing={1}
      value={theme}
      onValueChange={(value) => {
        if (value) setTheme(value as Theme)
      }}
      aria-label="Theme"
      className="theme-switch grid w-36 grid-cols-2 gap-1 rounded-sm border border-border bg-surface p-1"
    >
      <ToggleGroupItem value="light" aria-label="Light theme" className="h-auto rounded-sm px-2 py-2 font-mono text-xs uppercase tracking-wider text-fg-muted data-[state=on]:bg-surface-alt data-[state=on]:text-fg data-[state=off]:hover:bg-transparent data-[state=off]:hover:text-fg-muted data-[state=on]:hover:bg-surface-alt data-[state=on]:hover:text-fg">
        Light
      </ToggleGroupItem>
      <ToggleGroupItem value="dark" aria-label="Dark theme" className="h-auto rounded-sm px-2 py-2 font-mono text-xs uppercase tracking-wider text-fg-muted data-[state=on]:bg-surface-alt data-[state=on]:text-fg data-[state=off]:hover:bg-transparent data-[state=off]:hover:text-fg-muted data-[state=on]:hover:bg-surface-alt data-[state=on]:hover:text-fg">
        Dark
      </ToggleGroupItem>
    </ToggleGroup>
  )
}
