import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The browser requests /api/... from localhost:5173; Vite forwards it to
      // the FastAPI server on 8000. Two consequences worth knowing:
      //
      // 1. No CORS. As far as the browser is concerned there is one origin, so
      //    no preflight requests and no Access-Control headers to get wrong.
      // 2. No API base URL in the frontend code. api.ts just fetches "/api/...",
      //    which is also the correct path in production if the API and the
      //    static files are served from the same host.
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
