import { defineConfig } from "vite";
import { configDefaults } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
    // TypeScript before JavaScript (Vite's default puts .js first), so a stray
    // compiled .js left beside a source by an old `tsc -b` can never shadow it
    // in dev, build or tests.
    extensions: [".mts", ".ts", ".tsx", ".mjs", ".js", ".jsx", ".json"],
  },
  server: {
    port: 5173,
  },
  test: {
    globals: true,
    // Default environment is `node` for schema / hook-logic tests that don't
    // touch the DOM. Component and TanStack-Query hook tests opt into `jsdom`
    // via a `// @vitest-environment jsdom` pragma at the top of the file
    // (see api/stations.test.tsx and api/matcher.test.tsx for the pattern).
    environment: "node",
    // Never collect compiled copies of the tests (src/**/*.js is gitignored
    // build output, not source).
    exclude: [...configDefaults.exclude, "src/**/*.js"],
    alias: { "@": path.resolve(__dirname, "./src") },
    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary", "html"],
      // Measure frontend source only; exclude tests, generated bundles,
      // and the top-level entrypoint (main.tsx wires routing / providers —
      // covered implicitly by the hook/component tests and not worth
      // unit-testing in isolation).
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/main.tsx", "src/vite-env.d.ts"],
    },
  },
});
