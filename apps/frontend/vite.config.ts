import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Pinned so the dev server is predictable and never silently moves to
  // another port. 5173 (Vite's default) is used by another local project.
  server: {
    port: 5180,
    strictPort: true,
  },
});
