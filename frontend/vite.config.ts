import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Backend URL - configurable for Docker environments
const BACKEND_URL = process.env.VITE_BACKEND_URL || 'http://localhost:8000'
const WS_BACKEND_URL = BACKEND_URL.replace(/^http/, 'ws')

// https://vite.dev/config/
export default defineConfig({
  plugins: [svelte()],
  server: {
    host: true, // Allow access from Docker network
    proxy: {
      '/api': {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      '/ws': {
        target: WS_BACKEND_URL,
        ws: true,
      },
    },
  },
})
