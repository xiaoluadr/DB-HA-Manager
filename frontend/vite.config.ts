import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const manualChunks = {
  react: ['react', 'react-dom', 'react-router-dom'],
  antd: ['antd', '@ant-design/icons'],
  charts: ['echarts', 'echarts-for-react'],
  state: ['zustand'],
  utils: ['axios', 'dayjs'],
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    react({
      fastRefresh: true,
    }),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  optimizeDeps: {
    include: [
      'react',
      'react-dom',
      'react-router-dom',
      'antd',
      '@ant-design/icons',
      'axios',
      'zustand',
      'echarts',
      'echarts-for-react',
      'dayjs',
    ],
    esbuildOptions: {
      target: 'es2020',
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks,
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 3000,
    hmr: {
      port: 3000,
    },
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
})
