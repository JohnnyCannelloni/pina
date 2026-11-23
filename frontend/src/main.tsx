import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// leaflet's stylesheet, before ours so styles.css can override it.
import "leaflet/dist/leaflet.css";

import { App } from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root not found in index.html");
}

createRoot(container).render(
  // strict mode double-runs effects in dev; the fetch effects guard against it.
  <StrictMode>
    <App />
  </StrictMode>,
);
