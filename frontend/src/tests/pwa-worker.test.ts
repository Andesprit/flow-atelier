import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

function worker() {
  const handlers = new Map();
  const put = vi.fn();
  const caches = { open: async () => ({ put, match: async () => "offline shell" }) };
  runInNewContext(readFileSync("public/sw.js", "utf8"), {
    self: { location: { origin: "https://atelier.test" }, addEventListener: handlers.set.bind(handlers) },
    caches, URL, Response, fetch: vi.fn().mockRejectedValue(new Error("offline")),
  });
  return { handlers, put };
}

it("stores only a built-in offline page and leaves private requests to the network", async () => {
  const { handlers, put } = worker();
  let pending;
  handlers.get("install")({ waitUntil: (p: Promise<void>) => { pending = p; } });
  await pending;
  expect(put).toHaveBeenCalledTimes(1);
  for (const path of ["/flows", "/conduits", "/auth", "/ws", "/api/chat", "/dashboard?token=x"]) {
    const respondWith = vi.fn();
    handlers.get("fetch")({ request: new Request("https://atelier.test" + path), respondWith });
    expect(respondWith).not.toHaveBeenCalled();
  }
  const respondWith = vi.fn();
  handlers.get("fetch")({ request: { url: "https://atelier.test/dashboard", mode: "navigate",
    method: "GET", headers: new Headers() }, respondWith });
  expect(await respondWith.mock.calls[0][0]).toBe("offline shell");
  expect(put).toHaveBeenCalledTimes(1);
});

