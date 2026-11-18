import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import { synapseVite } from "@nimblebrain/synapse/vite";

// One inlined HTML file (vite-plugin-singlefile), so the server can serve the whole app as one
// HTML file, which it returns under each `ui://people/<view>`.
//
// `npm run dev` previews the views at /__preview against the real server: synapseVite() starts
// the local runtime over stdio, on its own database under ui/.preview/, and proxies the views'
// tool calls to it. The app name comes from the bundle's manifest.
export default defineConfig({
  plugins: [
    react(),
    viteSingleFile(),
    synapseVite({
      manifest: "../mcpb/manifest.json",
      serverCmd: "env PEOPLE_LOCAL_DB=.preview/crm.db uv run --project .. agentreadycrm",
    }),
  ],
  build: {
    outDir: "dist",
    assetsInlineLimit: Infinity,
  },
});
