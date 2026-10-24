import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built into the Python package; FastAPI serves index.html at / and the files under /assets.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/nightshift/web/static/app", emptyOutDir: true },
  // `npm run dev` needs the API running: `uv run nightshift serve` in another terminal.
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
