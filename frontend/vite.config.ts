import react from "@vitejs/plugin-react-swc"
import { defineConfig } from "vite"

// https://vitejs.dev/config/
export default defineConfig({
  server: {
    host: true,
    port: 5173,
    // Poll for changes — inotify events don't reliably cross the host→container
    // bind mount, so without this HMR misses edits to bind-mounted source.
    watch: { usePolling: true },
  },
  plugins: [react()],
})
