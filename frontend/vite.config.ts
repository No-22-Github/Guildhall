import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    open: false,
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.GUILDHALL_API_URL || 'http://127.0.0.1:8420',
        changeOrigin: true,
      },
    },
  },
})
