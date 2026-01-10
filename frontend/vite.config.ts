import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Backend URL from environment (for E2E tests) or default
const backendUrl = process.env.VITE_BACKEND_URL || 'http://localhost:8000';
const wsUrl = backendUrl.replace('http://', 'ws://').replace('https://', 'wss://');

// https://vite.dev/config/
export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: {
      '/api': {
        target: backendUrl,
        changeOrigin: true,
      },
      '/ws': {
        target: wsUrl,
        ws: true,
      },
    },
  },
})
