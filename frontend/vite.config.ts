import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built into the Python package; FastAPI serves index.html at /app and the assets from /static/app.
export default defineConfig({
  plugins: [react()],
  base: "/static/app/",
  build: { outDir: "../src/nightshift/web/static/app", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000", "/jobs": "http://127.0.0.1:8000" } },
});
