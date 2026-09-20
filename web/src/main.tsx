import React from 'react'
import ReactDOM from 'react-dom/client'

// Fonts are self-hosted by the canonical 42labs Tailwind bridge.
import '@fontsource-variable/space-grotesk'
import '@fontsource-variable/ibm-plex-sans'
import '@fontsource-variable/geist-mono'

// Import order is load-bearing: bridge.css pulls in the design-system tokens from
// the installed package first — raw palette and semantic vars — then aliases the
// shadcn names on top of them; Tailwind's layers come last so utilities see the
// bridge vars.
import '@/bridge.css'
import '@/index.css'

import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
