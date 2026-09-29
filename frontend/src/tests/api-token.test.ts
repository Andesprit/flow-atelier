import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { getApiToken, setApiToken } from "@/config/env";
import { fetchJson, resetTokenPrompt } from "@/services/client";

/**
 * The token is read at runtime, not compiled in: the SPA that ships in the
 * wheel is built once for everyone, so a build-time constant is empty in every
 * installed copy and the bundled UI could never authenticate against a server
 * started with ATELIER_API_TOKEN.
 */
describe("runtime API token", () => {
  beforeEach(() => {
    sessionStorage.clear();
    resetTokenPrompt();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("round-trips through sessionStorage", () => {
    expect(getApiToken()).toBe("");
    setApiToken("s3cret");
    expect(getApiToken()).toBe("s3cret");
  });

  it("sends no Authorization header when no token is set", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await fetchJson("http://x/conduits", undefined, { method: "GET" });
    const headers = fetchMock.mock.calls[0][1].headers;
    expect(headers.Authorization).toBeUndefined();
  });

  it("sends a stored token as a bearer header", async () => {
    setApiToken("s3cret");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await fetchJson("http://x/conduits", undefined, { method: "GET" });
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer s3cret");
  });

  it("prompts on a 401, stores the token, and retries once", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("nope", { status: 401 }))
      .mockResolvedValueOnce(
        new Response('{"ok":true}', {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("prompt", vi.fn().mockReturnValue("from-user"));

    const body = await fetchJson<{ ok: boolean }>("http://x/conduits", undefined, {
      method: "GET",
    });

    expect(body.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe("Bearer from-user");
    // Kept for the tab, so the next call and the WS handshake don't re-ask.
    expect(getApiToken()).toBe("from-user");
  });

  it("does not re-prompt in a loop when the entered token is also rejected", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("nope", { status: 401 }));
    const promptMock = vi.fn().mockReturnValue("wrong");
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("prompt", promptMock);

    await expect(
      fetchJson("http://x/conduits", undefined, { method: "GET" }),
    ).rejects.toThrow("API error 401");
    expect(promptMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("turns a validation error body into readable messages", async () => {
    const body = JSON.stringify({
      detail: [{ loc: ["body", "tasks", 0, "until"], msg: "predicate must start with 'output.match('" }],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 422 })));
    await expect(
      fetchJson("http://x/conduits/ship", undefined, { method: "PATCH" }),
    ).rejects.toThrow(/^API error 422: predicate must start with 'output.match\('$/);
  });

  it("lets the 401 stand when the user dismisses the prompt", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("nope", { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("prompt", vi.fn().mockReturnValue(null));

    await expect(
      fetchJson("http://x/conduits", undefined, { method: "GET" }),
    ).rejects.toThrow("API error 401");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  // The dashboard fires several fetches on mount (conduits, schedules, flows).
  // With a token-protected server they all 401 together, and asking once per
  // request means the user answers the same box three times.
  it("asks once for a burst of concurrent 401s", async () => {
    const promptMock = vi.fn().mockReturnValue("from-user");
    vi.stubGlobal("prompt", promptMock);
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init: RequestInit) => {
        const auth = (init.headers as Record<string, string>).Authorization;
        return Promise.resolve(
          auth === "Bearer from-user"
            ? new Response('{"ok":true}', {
                status: 200,
                headers: { "content-type": "application/json" },
              })
            : new Response("nope", { status: 401 }),
        );
      }),
    );

    const results = await Promise.all([
      fetchJson<{ ok: boolean }>("http://x/conduits", undefined, { method: "GET" }),
      fetchJson<{ ok: boolean }>("http://x/schedules", undefined, { method: "GET" }),
      fetchJson<{ ok: boolean }>("http://x/flows", undefined, { method: "GET" }),
    ]);

    expect(results.every((r) => r.ok)).toBe(true);
    expect(promptMock).toHaveBeenCalledTimes(1);
  });

  it("does not re-ask each request after the user dismisses once", async () => {
    // A fresh Response per call: three callers each read the error body, and a
    // body can only be consumed once.
    const fetchMock = vi.fn(() =>
      Promise.resolve(new Response("nope", { status: 401 })),
    );
    const promptMock = vi.fn().mockReturnValue(null);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("prompt", promptMock);

    const attempts = [
      fetchJson("http://x/conduits", undefined, { method: "GET" }),
      fetchJson("http://x/schedules", undefined, { method: "GET" }),
      fetchJson("http://x/flows", undefined, { method: "GET" }),
    ].map((p) => expect(p).rejects.toThrow("API error 401"));
    await Promise.all(attempts);

    // Declining leaves the tab unauthenticated until reload — but it only
    // costs the user one dismissal, not one per in-flight request.
    expect(promptMock).toHaveBeenCalledTimes(1);
  });
});
