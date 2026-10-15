import { defineConfig } from "vite";
export default defineConfig({
  build: { outDir: "../backend/static", emptyOutDir: false, manifest: true },
  server: {
    host: "0.0.0.0",
    allowedHosts: [".e2b.app", "localhost", "127.0.0.1"],
    proxy: {
      "/api": {
        target: process.env.MUSIC_INFO_API || "http://10.0.0.77:8003",
        changeOrigin: true,
      },
    },
  },
});
