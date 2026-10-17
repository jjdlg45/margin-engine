/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      ink: "rgb(var(--ink) / <alpha-value>)",
      paper: "rgb(var(--paper) / <alpha-value>)",
      card: "rgb(var(--card) / <alpha-value>)",
      inset: "rgb(var(--inset) / <alpha-value>)",
      petrol: "rgb(var(--petrol) / <alpha-value>)",
      verdigris: "rgb(var(--verdigris) / <alpha-value>)",
      amber: "rgb(var(--amber) / <alpha-value>)",
      garnet: "rgb(var(--garnet) / <alpha-value>)",
      white: "#ffffff",
    },
    fontFamily: {
      sans: ['"IBM Plex Sans Variable"', "system-ui", "sans-serif"],
      mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
    },
    extend: {
      fontSize: {
        display: ["2.25rem", { lineHeight: "1.15", fontWeight: "600" }],
        title: ["1.5rem", { lineHeight: "1.25", fontWeight: "600" }],
        section: ["1.17rem", { lineHeight: "1.3", fontWeight: "600" }],
        caption: ["0.78rem", { lineHeight: "1.35" }],
      },
      borderRadius: { card: "6px", badge: "3px" },
    },
  },
  plugins: [],
};
