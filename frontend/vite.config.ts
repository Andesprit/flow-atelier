/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

const publicEnvPrefixes = ["VITE_BACKEND_URL", "VITE_USE_MOCK_API"];

export default defineConfig(({ command, mode }) => {
  // Mirror the env resolution in src/config/env.ts. A build warns loudly when it
  // would ship mock data or pin the backend to localhost, since either silently
  // breaks a real deployment.
  const env = loadEnv(mode, process.cwd(), publicEnvPrefixes);
  const useMock = (env.VITE_USE_MOCK_API ?? "false") === "true";
  // An unset VITE_BACKEND_URL means same-origin (the SPA is served by the
  // backend), which is correct for a deployed build — so only warn when an
  // explicit override pins the backend to localhost.
  const baseUrl = env.VITE_BACKEND_URL;
  const isLocalhost =
    !!baseUrl && /^https?:\/\/(localhost|127\.0\.0\.1)([:/]|$)/.test(baseUrl);

  if (command === "build" && useMock) {
    console.warn(
      "\n⚠️  BUILD WARNING: VITE_USE_MOCK_API is true.\n" +
        "   This build will serve FAKE mock data instead of a real backend.\n" +
        "   Set VITE_USE_MOCK_API=false to build for real use.\n",
    );
  }
  if (command === "build" && isLocalhost) {
    console.warn(
      `\n⚠️  BUILD WARNING: VITE_BACKEND_URL is ${baseUrl} (localhost).\n` +
        "   A deployed build can't reach the machine it was built on.\n" +
        "   Leave VITE_BACKEND_URL unset to use the serving origin, or point it at the real backend.\n",
    );
  }

  return {
    // Only expose the public settings in bundles, regardless of the build mode.
    envPrefix: command === "build" ? publicEnvPrefixes : "VITE_",
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
        "@components": path.resolve(__dirname, "./src/components"),
        "@pages": path.resolve(__dirname, "./src/pages"),
        "@features": path.resolve(__dirname, "./src/features"),
        "@services": path.resolve(__dirname, "./src/services"),
        "@types": path.resolve(__dirname, "./src/types"),
        "@hooks": path.resolve(__dirname, "./src/hooks"),
        "@utils": path.resolve(__dirname, "./src/utils"),
        "@layout": path.resolve(__dirname, "./src/layout"),
        "@context": path.resolve(__dirname, "./src/context"),
        "@config": path.resolve(__dirname, "./src/config"),
        "@constants": path.resolve(__dirname, "./src/constants"),
        "@lib": path.resolve(__dirname, "./src/lib"),
      },
    },
    test: {
      include: ["src/**/*.test.{ts,tsx}"],
      // Without this, vitest runs in the node environment and every test that
      // touches localStorage or the DOM throws.
      environment: "jsdom",
      // Restores localStorage, which Node's own experimental global shadows.
      setupFiles: ["./vitest.setup.ts"],
    },
    server: {
      port: 5173,
      strictPort: true,
      watch: {
        ignored: [
          "**/.claude/**",
          "**/.superpowers/**",
          "**/.playwright-cli/**",
          "**/docs/**",
          "**/*.tsbuildinfo",
        ],
      },
    },
  };
});
