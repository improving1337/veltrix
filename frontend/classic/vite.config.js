import {defineConfig} from 'vite'
import tailwind from '@tailwindcss/vite'

export default defineConfig({
  base: '/assets/classic/',
  plugins: [tailwind()],
  esbuild: {jsx: 'automatic'},
  build: {
    outDir: '../../web/assets/classic',
    emptyOutDir: true,
    rolldownOptions: {checks: {moduleLevelDirective: false}},
  },
})
