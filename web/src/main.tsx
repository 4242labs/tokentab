import React from 'react'
import ReactDOM from 'react-dom/client'

// Fonts are self-hosted by the canonical 42labs Tailwind bridge.
import '@fontsource-variable/space-grotesk'
import '@fontsource-variable/ibm-plex-sans'
import '@fontsource-variable/geist-mono'

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
