import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" + hash routing keeps the build portable to GitHub Pages
// regardless of the repository name.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { sourcemap: false },
});
