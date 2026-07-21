import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Tauri looks at the frontend on a fixed port during dev — must match the
// `devUrl` in src-tauri/tauri.conf.json. clearScreen is off so Rust compile
// errors from the tauri CLI aren't wiped by Vite's own screen clears.
export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: false,
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    // Match the target the Tauri CLI documents for Windows 10+.
    target: "esnext",
    minify: !process.env.TAURI_ENV_DEBUG ? "esbuild" : false,
    sourcemap: !!process.env.TAURI_ENV_DEBUG,
  },
});
