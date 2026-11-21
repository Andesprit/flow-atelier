import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PushToggle } from "@/features/dashboard/components/push-toggle";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("asks for permission only after Enable and leaves a denied device unsubscribed", async () => {
  const requestPermission = vi.fn().mockResolvedValue("denied");
  vi.stubGlobal("Notification", { permission: "default", requestPermission });
  vi.stubGlobal("PushManager", {});
  const register = vi.fn();
  vi.stubGlobal("navigator", { serviceWorker: { getRegistration: async () => undefined, register } });
  render(<PushToggle />);
  expect(requestPermission).not.toHaveBeenCalled();
  const enable = screen.getByRole<HTMLButtonElement>("button", { name: "Enable completion notifications" });
  await waitFor(() => expect(enable.disabled).toBe(false));
  fireEvent.click(enable);
  await screen.findByText(/blocked in your browser settings/i);
  expect(register).not.toHaveBeenCalled();
});


it("shows the last delivery failure for this device", async () => {
  vi.stubGlobal("Notification", {});
  vi.stubGlobal("PushManager", {});
  vi.stubGlobal("navigator", { serviceWorker: { getRegistration: async () => ({
    pushManager: { getSubscription: async () => ({ endpoint: "https://push.example/device" }) },
  }) } });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"last_error":"Push rejected (HTTP 403)"}')));
  render(<PushToggle />);
  await screen.findByText(/HTTP 403/);
  expect(screen.getByText(/every project/i)).toBeTruthy();
});
