import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PushToggle } from "@/features/dashboard/components/push-toggle";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("asks for permission only after Enable and leaves a denied device unsubscribed", async () => {
  const requestPermission = vi.fn().mockResolvedValue("denied");
  vi.stubGlobal("Notification", { permission: "default", requestPermission });
  vi.stubGlobal("PushManager", {});
  const register = vi.fn();
  vi.stubGlobal("navigator", { serviceWorker: { getRegistration: async () => undefined, register } });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ key: "public-key" })));
  render(<PushToggle />);
  expect(requestPermission).not.toHaveBeenCalled();
  const enable = await screen.findByRole<HTMLButtonElement>("button", { name: "Enable completion notifications" });
  await waitFor(() => expect(enable.disabled).toBe(false));
  fireEvent.click(enable);
  await screen.findByText(/blocked in your browser settings/i);
  expect(register).not.toHaveBeenCalled();
});

it("hides Enable when the server returns 503 for its push key", async () => {
  const requestPermission = vi.fn();
  vi.stubGlobal("Notification", { requestPermission });
  vi.stubGlobal("PushManager", {});
  vi.stubGlobal("navigator", { serviceWorker: { getRegistration: async () => undefined } });
  const request = vi.fn(async () => Response.json({ detail: "Web Push unavailable" }, { status: 503 }));
  vi.stubGlobal("fetch", request);

  render(<PushToggle />);
  await act(async () => {});

  expect(request).toHaveBeenCalledWith(expect.stringMatching(/\/push\/key$/), expect.objectContaining({ method: "GET" }));
  expect(screen.queryByRole("button", { name: "Enable completion notifications" })).toBeNull();
  expect(requestPermission).not.toHaveBeenCalled();
});

it("subscribes and unsubscribes when push is available", async () => {
  const requestPermission = vi.fn().mockResolvedValue("granted");
  const unsubscribe = vi.fn().mockResolvedValue(true);
  const endpoint = "https://push.example/device";
  const subscribe = vi.fn().mockResolvedValue({
    endpoint,
    toJSON: () => ({ endpoint, keys: { auth: "auth", p256dh: "p256dh" } }),
    unsubscribe,
  });
  const register = vi.fn();
  vi.stubGlobal("Notification", { requestPermission });
  vi.stubGlobal("PushManager", {});
  vi.stubGlobal("navigator", { serviceWorker: {
    getRegistration: async () => undefined,
    register,
    ready: Promise.resolve({ pushManager: { subscribe } }),
  } });
  const request = vi.fn(async (url: string) => {
    if (url.endsWith("/push/key")) return Response.json({ key: "public-key" });
    if (url.endsWith("/push/status")) return Response.json({ last_error: null });
    return new Response(null, { status: 204 });
  });
  vi.stubGlobal("fetch", request);

  render(<PushToggle />);
  const enable = await screen.findByRole<HTMLButtonElement>("button", { name: "Enable completion notifications" });
  await waitFor(() => expect(enable.disabled).toBe(false));
  fireEvent.click(enable);

  const disable = await screen.findByRole("button", { name: "Disable completion notifications" });
  expect(register).toHaveBeenCalledWith("/sw.js");
  expect(subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: "public-key" });
  expect(request).toHaveBeenCalledWith(expect.stringMatching(/\/push\/subscriptions$/), expect.objectContaining({ method: "POST" }));
  fireEvent.click(disable);

  await screen.findByRole("button", { name: "Enable completion notifications" });
  expect(unsubscribe).toHaveBeenCalledOnce();
  expect(request).toHaveBeenCalledWith(expect.stringMatching(/\/push\/subscriptions$/), expect.objectContaining({ method: "DELETE" }));
});

it("shows the last delivery failure for this device", async () => {
  vi.stubGlobal("Notification", {});
  vi.stubGlobal("PushManager", {});
  vi.stubGlobal("navigator", { serviceWorker: { getRegistration: async () => ({
    pushManager: { getSubscription: async () => ({ endpoint: "https://push.example/device" }) },
  }) } });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/push/key")
    ? Response.json({ key: "public-key" })
    : Response.json({ last_error: "Push rejected (HTTP 403)" })));
  render(<PushToggle />);
  await screen.findByText(/HTTP 403/);
  expect(screen.getByText(/every project/i)).toBeTruthy();
});
