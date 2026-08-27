import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs'
import path from 'node:path'

/** The demo fixture sits in public/ so the app can just fetch it, but public/ is
 *  copied wholesale — so a production build run on a machine where `build:demo`
 *  ran earlier would quietly ship a megabyte of demo data. Drop it. */
const dropDemoFixture = (mode: string): Plugin => ({
  name: 'drop-demo-fixture',
  apply: 'build',
  closeBundle() {
    if (mode !== 'demo') fs.rmSync(path.resolve(import.meta.dirname, 'dist/demo.json'), { force: true })
  },
})

// `base: './'` (relative) so one bundle works served at a domain root or mounted
// at a subpath. Output goes to ./dist, which tokentab.py serves directly
// (WEB_DIR, override with TOKENTAB_WEB) — the serving host needs no Node.
//
// The dev proxy points `npm run dev` at a local `tokentab serve` on :8899.
export default defineConfig(({ mode }) => ({
  base: process.env.VITE_BASE || './',
  plugins: [react(), tailwindcss(), dropDemoFixture(mode)],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
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
}))
