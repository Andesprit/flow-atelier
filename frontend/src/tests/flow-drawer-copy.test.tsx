import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { FlowDrawer } from "@/components/FlowDrawer";
import type { LogEntry } from "@/types/task";

const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: { error: (...args: unknown[]) => toastError(...args) },
}));

// Radix's ScrollArea observes its viewport; jsdom ships no ResizeObserver.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

const FLOW_ID = "20260911_53ac80af_goal_loop";

// Local-time construction keeps the expected HH:MM:SS stable across timezones.
const T = new Date(2026, 8, 11, 9, 5, 7).getTime();
const lines: LogEntry[] = [
  { t: T, text: "▸ flow started", level: "info" },
  { t: T + 1000, text: "▸ setup", level: "info", task: "setup" },
  { t: T + 2000, text: "1789121700", level: "ok", task: "setup" },
];

// jsdom has no clipboard; install (or remove) one per test.
function installClipboard(writeText?: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  });
}

/**
 * The flow id is what `atelier logs/status/stop <id>` take and what names the
 * run folder on disk, and the log text is what gets pasted into an issue. Both
 * have to leave the drawer in one click, and a copy that did not happen must
 * never read as one that did.
 */
describe("FlowDrawer copy actions", () => {
  beforeEach(() => toastError.mockReset());
  afterEach(cleanup);

  it("shows the flow id", () => {
    installClipboard(vi.fn().mockResolvedValue(undefined));
    render(<FlowDrawer open onClose={() => {}} title="goal_loop" badge="done" flowId={FLOW_ID} />);
    expect(screen.getByTestId("flow-drawer-id").textContent).toContain(FLOW_ID);
  });

  it("copies the flow id and confirms", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    installClipboard(writeText);
    render(<FlowDrawer open onClose={() => {}} title="goal_loop" flowId={FLOW_ID} />);
    fireEvent.click(screen.getByTestId("flow-drawer-copy-id"));
    expect(writeText).toHaveBeenCalledWith(FLOW_ID);
    await waitFor(() =>
      expect(screen.getByTestId("flow-drawer-copy-id").textContent).toBe("copied"),
    );
  });

  it("copies every log line in order, including the task-tagged ones the flat log box hides", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    installClipboard(writeText);
    render(<FlowDrawer open onClose={() => {}} title="goal_loop" badge="done" logLines={lines} />);
    fireEvent.click(screen.getByTestId("flow-drawer-copy-logs"));
    expect(writeText).toHaveBeenCalledWith(
      "09:05:07 ▸ flow started\n09:05:08 [setup] ▸ setup\n09:05:09 [setup] 1789121700",
    );
  });

  it("renders neither control when there is nothing to copy", () => {
    render(<FlowDrawer open onClose={() => {}} title="goal_loop" badge="done" />);
    expect(screen.queryByTestId("flow-drawer-id")).toBeNull();
    expect(screen.queryByTestId("flow-drawer-copy-logs")).toBeNull();
  });

  it("explains itself when the clipboard API is unavailable", () => {
    // Plain http on a LAN address is not a secure context, so the API is absent.
    installClipboard(undefined);
    render(<FlowDrawer open onClose={() => {}} title="goal_loop" flowId={FLOW_ID} />);
    fireEvent.click(screen.getByTestId("flow-drawer-copy-id"));
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("flow-drawer-copy-id").textContent).toBe("copy id");
  });

  it("reports a rejected clipboard write instead of claiming success", async () => {
    installClipboard(vi.fn().mockRejectedValue(new Error("denied")));
    render(<FlowDrawer open onClose={() => {}} title="goal_loop" flowId={FLOW_ID} />);
    fireEvent.click(screen.getByTestId("flow-drawer-copy-id"));
    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("flow-drawer-copy-id").textContent).toBe("copy id");
  });
});
