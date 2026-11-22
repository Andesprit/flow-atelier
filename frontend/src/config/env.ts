/**
 * App configuration from Vite env vars. Values in `.env` / `.env.local` override
 */
// Default to the real API; mocks are opt-in via VITE_USE_MOCK_API=true.
export const USE_MOCK = import.meta.env.VITE_USE_MOCK_API === "true";
// The bundled SPA is served by the FastAPI app itself, so the backend lives
// at whatever origin the page loaded from. Deriving BASE_URL at runtime keeps
// the frontend in sync with any `atelier serve --port` automatically.
// VITE_BACKEND_URL overrides this (e.g. a dev server on a separate port).
export const BASE_URL = import.meta.env.VITE_BACKEND_URL ?? window.location.origin;
// The bundled SPA gets its token through runtime sign-in.
let bearer = import.meta.env.DEV ? (import.meta.env.VITE_API_TOKEN ?? "") : "";

export function getApiToken(): string {
  return bearer;
}

export function setApiToken(token: string): void {
  bearer = token;
}
