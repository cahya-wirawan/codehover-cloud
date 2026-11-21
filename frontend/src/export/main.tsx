import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource-variable/newsreader/opsz.css";
import "@fontsource-variable/newsreader/opsz-italic.css";
import "@fontsource/red-hat-mono/400.css";
import "@fontsource/red-hat-mono/500.css";
import "@fontsource/red-hat-mono/600.css";
import "../styles/index.css";
import "../styles/viewer.css";
import { ExportApp } from "./ExportApp";
import type { Kb } from "./staticSource";

// data/data.js sets this before the module runs; it is the build's knowledge base, nothing else.
const kb = (window as unknown as { CODEHOVER_DATA?: Kb }).CODEHOVER_DATA;
const root = document.getElementById("root");
if (root && kb) {
  document.title = kb.project.name;
  createRoot(root).render(
    <StrictMode>
      <ExportApp kb={kb} />
    </StrictMode>,
  );
} else if (root) {
  root.textContent = "This copy's data is missing (data/data.js).";
}
