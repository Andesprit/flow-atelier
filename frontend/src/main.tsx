import ReactDOM from "react-dom/client";
import App from "./App";

// Fonts are bundled rather than pulled from fonts.googleapis.com: this UI is
// served by `atelier serve` on a developer's own machine, so it has to render
// offline and behind an air-gapped or egress-filtered network, and a CDN <link>
// would also report every page load to a third party. Only the latin subset and
// the weights actually used are imported.
import "@fontsource/young-serif/latin-400.css";
import "@fontsource/geist-sans/latin-300.css";
import "@fontsource/geist-sans/latin-400.css";
import "@fontsource/geist-sans/latin-500.css";
import "@fontsource/geist-sans/latin-600.css";
import "@fontsource/geist-sans/latin-700.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "@fontsource/jetbrains-mono/latin-600.css";

import "./styles/globals.css";

ReactDOM.createRoot(document.getElementById("root")!).render(<App />);

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {
    console.warn("Offline mode and Web Push are unavailable.");
  });
}
