/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" + hash routing keeps the build portable to GitHub Pages
// regardless of the repository name.
/** index.html preloads the static bundle's first files for paint speed; in
 * API mode (VITE_DATA_SOURCE=http) the app never fetches them, so the
 * preloads are stripped. */
const stripStaticPreloads = () => ({
  name: "strip-static-preloads",
  transformIndexHtml(html: string) {
    if (process.env.VITE_DATA_SOURCE !== "http") return html;
    return html
      .split("\n")
      .filter((line) => !(line.includes('rel="preload"') && line.includes("/data/")))
      .join("\n");
  },
});

export default defineConfig({
  plugins: [react(), stripStaticPreloads()],
  base: "./",
  server: {
    // API mode (Run 4, Phase 30): same-origin /api proxied to the BFF so the
    // http adapter's default base URL works without CORS. Set by the API-mode
    // Playwright config and the "live mode" npm script.
    proxy: process.env.VITE_API_PROXY_TARGET
      ? { "/api": { target: process.env.VITE_API_PROXY_TARGET, changeOrigin: true } }
      : undefined,
  },
  test: {
    // Unit tests only - Playwright owns e2e/**.
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
  build: {
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          recharts: ["recharts"],
          ui: [
            "@radix-ui/react-tooltip",
            "@radix-ui/react-tabs",
            "@radix-ui/react-collapsible",
            "@radix-ui/react-slider",
            "@tanstack/react-table",
          ],
        },
      },
    },
  },
});
