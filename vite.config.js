import { defineConfig } from 'vite';

export default defineConfig({
  root: 'src',
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: 'src/index.html'
      }
    }
  },
  server: {
    port: 1420,
    strictPort: true
  },
  clearScreen: false,
  envPrefix: ['VITE_', 'TAURI_']
});

