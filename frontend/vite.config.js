import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :5173 forwards /api to the FastAPI backend.
// 127.0.0.1, not "localhost": on Windows (Node 18+) localhost can resolve to IPv6 ::1,
// while uvicorn listens on IPv4 127.0.0.1, so every /api call would be refused.
// Set API_TARGET to point elsewhere, e.g. API_TARGET=http://127.0.0.1:8020 npm run dev
const target = process.env.API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target, changeOrigin: true } },
  },
});
