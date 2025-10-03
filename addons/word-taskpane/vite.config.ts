import basicSsl from '@vitejs/plugin-basic-ssl';
import { defineConfig } from 'vite';

export default defineConfig({
  root: 'src',
  plugins: [basicSsl()],
  server: {
    host: 'localhost',
    port: 5174,
    https: true
  },
  preview: {
    host: 'localhost',
    port: 5175,
    https: true
  },
  build: {
    outDir: '../dist',
    emptyOutDir: true
  }
});
