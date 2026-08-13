import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// `base: './'` (relative) so one bundle works served at a domain root or mounted
// at a subpath. Output goes to ./dist, which tokentab.py serves directly
// (WEB_DIR, override with TOKENTAB_WEB) — the serving host needs no Node.
//
// The dev proxy points `npm run dev` at a local `tokentab serve` on :8899.
export default defineConfig({
  base: process.env.VITE_BASE || './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8899', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
