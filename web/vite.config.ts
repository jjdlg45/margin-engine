import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" + hash routing keeps the build portable to GitHub Pages
// regardless of the repository name.
export default defineConfig({
  plugins: [react()],
  base: "./",
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
