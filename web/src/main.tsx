import React from 'react'
import ReactDOM from 'react-dom/client'

// Fonts, self-hosted (no external/Google runtime request — the dashboard is
// tailnet-only and must render with no internet at all). Weights match
// ds-tokens.css: Space Grotesk (heading), IBM Plex Sans (body), Geist Mono (mono).
import '@fontsource/space-grotesk/500.css'
import '@fontsource/space-grotesk/600.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/geist-mono/400.css'
import '@fontsource/geist-mono/500.css'

// Import order is load-bearing: the vendored 42labs design-system tokens define
// the raw palette + semantic vars first, the shadcn<->token bridge aliases on
// top of those, then Tailwind's layers last so utilities see the bridge vars.
import '@/ds-tokens.css'
import '@/bridge.css'
import '@/index.css'

import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
