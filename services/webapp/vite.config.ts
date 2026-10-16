import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Клиент ходит на относительные /api/... и /ws; в dev их проксирует Vite.
const SERVER_URL = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": SERVER_URL,
      "/ws": { target: SERVER_URL, ws: true },
    },
  },
  test: {
    // Глобальные test/expect нужны, чтобы Testing Library сам чистил DOM
    // после каждого теста (он вешает cleanup на глобальный afterEach).
    globals: true,
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
  },
});
