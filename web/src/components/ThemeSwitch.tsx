import { useEffect, useState } from 'react'

import { ThemeSwitch as CanonicalThemeSwitch } from '@4242labs/design-system/components/theme-switch'

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

  return <CanonicalThemeSwitch theme={theme} setTheme={setTheme} className="w-20" />
}
