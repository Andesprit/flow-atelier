import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

function worker() {
  const handlers = new Map();
  const put = vi.fn();
  const showNotification = vi.fn();
  const openWindow = vi.fn();
  const caches = { open: async () => ({ put, match: async () => "offline shell" }) };
  runInNewContext(readFileSync("public/sw.js", "utf8"), {
    self: { location: { origin: "https://atelier.test" }, addEventListener: handlers.set.bind(handlers),
      registration: { showNotification }, clients: { openWindow } },
    caches, URL, Response, fetch: vi.fn().mockRejectedValue(new Error("offline")),
  });
  return { handlers, put, showNotification, openWindow };
}

it("stores only a built-in offline page and leaves private requests to the network", async () => {
  const { handlers, put } = worker();
  let pending;
  handlers.get("install")({ waitUntil: (p: Promise<void>) => { pending = p; } });
  await pending;
  expect(put).toHaveBeenCalledTimes(1);
  for (const path of ["/flows", "/conduits", "/auth", "/ws", "/api/chat", "/dashboard?token=x"]) {
    const respondWith = vi.fn();
    handlers.get("fetch")({ request: { url: "https://atelier.test" + path, method: "GET",
      mode: "navigate", headers: new Headers() }, respondWith });
    expect(respondWith).not.toHaveBeenCalled();
  }
  const respondWith = vi.fn();
  handlers.get("fetch")({ request: { url: "https://atelier.test/dashboard", mode: "navigate",
    method: "GET", headers: new Headers() }, respondWith });
  expect(await respondWith.mock.calls[0][0]).toBe("offline shell");
  expect(put).toHaveBeenCalledTimes(1);
});

it("displays the terminal status and opens only the matching local notice", () => {
  const { handlers, showNotification, openWindow } = worker();
  const run_id = "1234abcd".repeat(4);
  handlers.get("push")({ data: { json: () => ({ run_id, status: "failed" }) }, waitUntil: vi.fn() });
  expect(showNotification).toHaveBeenCalledWith("Run failed", expect.objectContaining({
    tag: run_id, data: { run_id },
  }));
  const click = (id: string) => handlers.get("notificationclick")({
    notification: { close: vi.fn(), data: { run_id: id } }, waitUntil: vi.fn(),
  });
  click("/untrusted");
  click(run_id);
  expect(openWindow).toHaveBeenCalledTimes(1);
  expect(openWindow).toHaveBeenCalledWith("/notice/" + run_id);
});

