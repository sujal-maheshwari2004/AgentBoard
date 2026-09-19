import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-mode proxy target: the whiteboard server port for the project you are testing against.
// Set WHITEBOARD_PORT in the environment when running `pnpm dev`.
const port = process.env.WHITEBOARD_PORT ?? '43000'

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: 'dist', sourcemap: false, emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: `ws://127.0.0.1:${port}`, ws: true },
      '/api': { target: `http://127.0.0.1:${port}`, changeOrigin: true },
      '/skeleton': { target: `http://127.0.0.1:${port}`, changeOrigin: true },
    },
  },
  test: { environment: 'jsdom', globals: true, setupFiles: ['./tests/setup.ts'] },
})
