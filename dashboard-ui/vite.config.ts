import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8899',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2022',
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            // Chart.js + react-chartjs-2 -- heaviest dep, isolate.
            if (id.includes('chart.js') || id.includes('react-chartjs-2')) return 'chartjs'
            // TanStack Query
            if (id.includes('@tanstack')) return 'tanstack'
            // React runtime + react-dom + scheduler
            if (
              id.includes('react-dom') ||
              id.includes('/react/') ||
              id.includes('scheduler')
            )
              return 'react-vendor'
          }
        },
      },
    },
  },
})
