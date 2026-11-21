import { BASE_URL, USE_MOCK, getApiToken, setApiToken } from "@/config/env";
import { toCamelCase, toSnakeCase } from "./transforms";

let login: Promise<boolean> | undefined;
let generation = 0;

export function authenticate(): Promise<boolean> {
  if (login) return login;
  if (declined || typeof window.prompt !== "function") return Promise.resolve(false);
  const entered = window.prompt("Enter this server’s API token (ATELIER_API_TOKEN).", "");
  if (!entered) {
    declined = true;
    return Promise.resolve(false);
  }
  login = (async () => {
    if (window.location.protocol === "https:" && window.location.origin === BASE_URL) {
      const response = await fetch(BASE_URL + "/session", {
        method: "POST", headers: { Authorization: `Bearer ${entered}` }, cache: "no-store",
      });
      if (!response.ok) throw new Error(`API error ${response.status}: Could not sign in.`);
      setApiToken("");
    } else {
      setApiToken(entered);
    }
    generation++;
    return true;
  })().finally(() => { login = undefined; });
  return login;
}

export async function forgetToken(): Promise<void> {
  await fetchJson(BASE_URL + "/session", undefined, { method: "DELETE" });
  setApiToken("");
  try {
    window.sessionStorage.removeItem("atelier:api-token");
  } catch { /* Storage may be disabled. */ }
  window.location.reload();
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
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
      ...(payload !== undefined ? { body: payload } : {}),
    });

  let res: Response;
  const before = generation;
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
    if (generation !== before || await authenticate()) {
      try {
        res = await send(getApiToken());
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
