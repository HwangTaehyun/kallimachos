import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwind from '@tailwindcss/vite';

// Declared here alone so @types/node is not added.  This is the only file that runs under Node and
// all it needs is env, so pulling in another package is not worth it.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react(), tailwind()],
  server: {
    host: true,          // it has to be 0.0.0.0 to be reachable from outside the container
    port: 5173,
    // During development vite proxies the API.  In production nginx does the same, so the frontend
    // code only ever uses the relative '/api' on both sides —— no per-environment branch.
    //  ⚠ `changeOrigin: false`.  With true, the Host becomes `api:8080` and the api's Host
    //     allowlist would have to include the container service name, which widens the attack
    //     surface.  Left as it is, the browser's loopback Host is forwarded and the allowlist
    //     stays narrow.  (2026-08-25 Round 3)
    proxy: { '/api': { target: process.env.KAL_API ?? 'http://api:8080', changeOrigin: false } },
  },
  build: { outDir: 'dist', sourcemap: false },
});
