import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";
import { NotifyToggle } from "@/layout/NotifyToggle";
import { NOTIFY_STORAGE_KEY } from "@/constants/dashboard";

// jsdom has no Notification API; only the two members the toggle reads.
const FakeNotification = {
  permission: "default" as NotificationPermission,
  requestPermission: vi.fn<() => Promise<NotificationPermission>>(),
};

const toggle = () => screen.getByTestId("notify-toggle");
const isOn = () => toggle().getAttribute("aria-pressed") === "true";
const click = () => act(async () => {
  fireEvent.click(toggle());
});

/**
 * The browser's permission prompt is the person's own click, never a run or
 * a page load; and turning it off is a local choice that leaves the browser
 * permission alone.
 */
describe("NotifyToggle", () => {
  beforeEach(() => {
    vi.stubGlobal("Notification", FakeNotification);
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("is off until asked, and asks the browser only then", async () => {
    FakeNotification.requestPermission.mockImplementation(async () => {
      FakeNotification.permission = "granted";
      return "granted";
    });
    render(<NotifyToggle />);
    expect(isOn()).toBe(false);
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
    await click();
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    expect(isOn()).toBe(true);
    expect(localStorage.getItem(NOTIFY_STORAGE_KEY)).toBe("1");
  });

  it("stays off when the browser refuses", async () => {
    FakeNotification.requestPermission.mockResolvedValue("denied");
    render(<NotifyToggle />);
    await click();
    expect(isOn()).toBe(false);
    expect(localStorage.getItem(NOTIFY_STORAGE_KEY)).toBeNull();
  });

  it("stays off when the prompt is dismissed", async () => {
    FakeNotification.requestPermission.mockResolvedValue("default");
    render(<NotifyToggle />);
    await click();
    expect(isOn()).toBe(false);
    expect(localStorage.getItem(NOTIFY_STORAGE_KEY)).toBeNull();
  });

  it("does not ask again once the browser has granted it", async () => {
    FakeNotification.permission = "granted";
    render(<NotifyToggle />);
    expect(isOn()).toBe(false);
    await click();
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
    expect(isOn()).toBe(true);
  });

  it("starts on when it was turned on before, and turns off without touching the browser", async () => {
    FakeNotification.permission = "granted";
    localStorage.setItem(NOTIFY_STORAGE_KEY, "1");
    render(<NotifyToggle />);
    expect(isOn()).toBe(true);
    await click();
    expect(isOn()).toBe(false);
    expect(localStorage.getItem(NOTIFY_STORAGE_KEY)).toBeNull();
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it("starts off when the browser permission was revoked meanwhile", () => {
    FakeNotification.permission = "denied";
    localStorage.setItem(NOTIFY_STORAGE_KEY, "1");
    render(<NotifyToggle />);
    expect(isOn()).toBe(false);
  });

  it("is absent where the browser has no Notification API", () => {
    vi.stubGlobal("Notification", undefined);
    render(<NotifyToggle />);
    expect(screen.queryByTestId("notify-toggle")).toBeNull();
  });
});
