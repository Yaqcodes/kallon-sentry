import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The gateway (gateway.py) serves the built SPA from ../web over loopback, at
// the site root. Relative asset URLs (base: './') keep it working no matter
// what path the kiosk browser opens. `npm run build` writes straight into the
// web root the installer already syncs to the Jetson, so there is no separate
// copy step and no Node on the device.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../web',
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` against a real tower: proxy the gateway API + SSE so the
    // dashboard behaves exactly as when served from the Jetson.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8766', changeOrigin: true },
      '/ingest': { target: 'http://127.0.0.1:8766', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8766', changeOrigin: true },
    },
  },
});
