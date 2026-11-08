import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Leaflet's own stylesheet. Without this import the map renders as a pile of
// mis-positioned tiles with no visible controls — the single most common
// "why is my Leaflet map broken" cause. It must come before our styles so
// styles.css can override it.
import "leaflet/dist/leaflet.css";

import { App } from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root not found in index.html");
}

createRoot(container).render(
  // StrictMode deliberately double-invokes effects in development to surface
  // missing cleanup. The `active` flags in App and DestinationPage are what
  // make the fetch effects survive that.
  <StrictMode>
    <App />
  </StrictMode>,
);
