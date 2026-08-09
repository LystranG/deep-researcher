import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": process.env.DEEP_RESEARCHER_API_TARGET ?? "http://127.0.0.1:8000",
      "/healthz": process.env.DEEP_RESEARCHER_API_TARGET ?? "http://127.0.0.1:8000"
    }
  },
  test: {
    exclude: [...configDefaults.exclude, "e2e/**"],
    environment: "jsdom",
    environmentOptions: {
      jsdom: { url: "http://localhost/" }
    },
    setupFiles: "./tests/setup.ts"
  }
});
