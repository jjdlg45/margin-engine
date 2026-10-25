// Fonts are self-hosted and preloaded from index.html (public/fonts/) so they
// are ready at first paint; importing them here bundled them behind the JS,
// and the late swap reflowed the page (layout shift).
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./theme.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
