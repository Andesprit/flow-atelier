import { BASE_URL, USE_MOCK, getApiToken, setApiToken } from "@/config/env";
import { toCamelCase, toSnakeCase } from "./transforms";

/**
 * Ask for the server's API token and remember it for this tab.
 *
 * `window.prompt` rather than a modal: this only fires when the server was
 * started with ATELIER_API_TOKEN, it needs no app state, and every REST call
 * already routes through `fetchJson` — a dialog would mean threading a
 * component through every call site for a path most users never hit.
 *
 * Returns "" when the user dismisses it, which lets the original 401 stand.
 */
function promptForToken(): string {
  if (declined || typeof window.prompt !== "function") return "";
  const entered = window.prompt(
    "This flow-atelier server requires an API token (ATELIER_API_TOKEN):",
    "",
  );
  if (!entered) {
    declined = true;
    return "";
  }
  setApiToken(entered);
  return entered;
}

// Set once the user dismisses the dialog. Without it a dismissal is re-asked
// per in-flight request, and the dashboard 401s several times on mount — the
// user would cancel the same box three times. Declining leaves the tab
// unauthenticated until it is reloaded, which is the point: they said no.
let declined = false;

/** Test seam: forget the dismissal so each case starts from a clean slate. */
export function resetTokenPrompt(): void {
  declined = false;
}

// FastAPI reports a rejected body as {"detail": [{"msg": ...}, ...]} or
// {"detail": "..."}; a raw dump of that JSON is unreadable in a toast.
function readableError(text: string): string {
  try {
    const { detail } = JSON.parse(text) as { detail?: unknown };
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (item as { msg?: unknown })?.msg)
        .filter((msg): msg is string => typeof msg === "string");
      if (messages.length > 0) return messages.join("; ");
    }
  } catch {
    // Not JSON: the text is already what the server said.
  }
  return text;
}

export async function fetchJson<TResponse>(
  url: string,
  body?: unknown,
  options?: {
    method?: string;
    headers?: Record<string, string>;
  },
): Promise<TResponse> {
  const { method = "POST", headers } = options ?? {};
  const payload =
    method !== "GET" && body !== undefined
      ? JSON.stringify(toSnakeCase(body))
      : undefined;

  // Token read per call, not captured at module load: it may not exist yet on
  // the first request and gets filled in by the 401 retry below.
  const send = (token: string) =>
    fetch(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
      ...(payload !== undefined ? { body: payload } : {}),
    });

  let res: Response;
  const sent = getApiToken();
  try {
    res = await send(sent);
  } catch {
    // A transport failure (server down, DNS, CORS) surfaces as a bare
    // "Failed to fetch", which tells the user nothing actionable.
    throw new Error(`Can't reach the flow-atelier API at ${BASE_URL}`);
  }
  // Retried once, never in a loop: a second 401 means the entered token is
  // wrong, and re-prompting on every rejection traps the user in a dialog.
  if (res.status === 401) {
    // A sibling request may have prompted while this one was in flight — the
    // dashboard fires several fetches on mount and they all 401 together. Reuse
    // whatever it stored instead of asking the same question once per request.
    const stored = getApiToken();
    const token = stored && stored !== sent ? stored : promptForToken();
    if (token) {
      try {
        res = await send(token);
      } catch {
        throw new Error(`Can't reach the flow-atelier API at ${BASE_URL}`);
      }
    }
  }
  if (!res.ok) {
    const errorText = await res.text();
    throw new Error(`API error ${res.status}: ${readableError(errorText)}`);
  }
  // A successful call may have no body (e.g. a 204 DELETE). Parsing an empty
  // string as JSON throws, so a successful delete would surface as an error.
  const text = await res.text();
  if (!text) return undefined as TResponse;
  try {
    return toCamelCase<TResponse>(JSON.parse(text));
  } catch {
    // A 200 that isn't JSON means we hit something other than the API — most
    // often an SPA index.html from a dev server with no backend behind it.
    // The raw SyntaxError ("Unexpected token '<', \"<!doctype \"...") used to
    // reach the user verbatim as the entire error message.
    throw new Error(
      `Expected JSON from ${url} but got ${res.headers.get("content-type") ?? "an unknown type"}. ` +
        `Is the flow-atelier API running at ${BASE_URL}?`,
    );
  }
}

export { USE_MOCK, BASE_URL };
