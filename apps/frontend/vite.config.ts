import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  // Pinned so the dev server is predictable and never silently moves to
  // another port. 5173 (Vite's default) is used by another local project.
  server: {
    port: 5180,
    strictPort: true,
    // Proxy the Mini App API to the FastAPI backend (BALAM_PORT) so `bun run dev`
    // (5180) and the served build hit the same relative `/api/*` paths.
    proxy: { "/api": "http://127.0.0.1:3000" },
  },
});
