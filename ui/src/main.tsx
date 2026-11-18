import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppProvider } from "@nimblebrain/synapse/react";
// Establish the root-height chain (html/body/#root) before React mounts so the
// AppFrame shell fills the pane with no first-paint jump. AppFrame also injects
// this on render; the entry import just applies it earlier. (synapse ≥ 0.11)
import "@nimblebrain/synapse/ui/base";
import App, { type View } from "./App";
import { FieldsSettings } from "./people/FieldsView";

// The server names the view on the root element (`src/mcp_people/ui.py`): each of
// the app's views, and its settings section, is the same bundle served under its
// own `ui://people/<view>`, which is what lets the host place each one. Unmarked
// (the dev preview, which serves `index.html` as written) is the contacts list.
const root = document.getElementById("root")!;
const VIEWS: Record<string, View> = {
  contacts: "contact",
  organizations: "organization",
  opportunities: "opportunity",
};
const view = root.dataset.view ?? "contacts";

createRoot(root).render(
  <StrictMode>
    <AppProvider name="people" version="0.1.1" forwardKeys>
      {view === "settings" ? <FieldsSettings /> : <App view={VIEWS[view] ?? "contact"} />}
    </AppProvider>
  </StrictMode>,
);
