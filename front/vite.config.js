import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig({
  // 与后端同源托管在根路径，保持 base 为 '/'
  base: '/',
  plugins: [
    vue(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  build: {
    // 构建产物直接输出到后端静态目录，由 FastAPI 同源托管，部署时随 static/ 一起上传
    outDir: '../static/app/dist',
    emptyOutDir: true,
  },
})
