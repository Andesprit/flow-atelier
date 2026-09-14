import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, cleanup } from "@testing-library/react";
import type { ServerWsMessage } from "@/types/ws";

// A stand-in socket the test drives directly, so a run can be started and
// finished without a server.
class FakeSocket {
  static last: FakeSocket | null = null;
  onMessage: (msg: ServerWsMessage) => void = () => {};
  onClose: (code: number, reason: string) => void = () => {};
  readyState = 1;

  constructor() {
    FakeSocket.last = this;
  }

  send() {}
  close() {}
  waitForOpen() {
    return Promise.resolve();
  }
}

vi.mock("@/services/api/run-conduit", () => ({
  RunConduitSocket: FakeSocket,
}));

const { useConduit } = await import("@/hooks/useConduit");

/**
 * Fire the event the browser sends before a reload, tab close or cross-site
 * navigation. A cancelled event is what makes the browser show its
 * leave-page prompt, so "blocked" here means "the user would be asked".
 */
function leaveIsBlocked(): boolean {
  const ev = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(ev);
  return ev.defaultPrevented;
}

/** Start `n` runs and have the server acknowledge each one. */
async function started(n = 1) {
  const hook = renderHook(() => useConduit());
  await act(async () => {
    for (let i = 0; i < n; i++) hook.result.current.run("c", {}, "/p");
  });
  const sock = FakeSocket.last!;
  act(() => {
    for (let i = 0; i < n; i++) sock.onMessage({ type: "started", flowId: `f${i + 1}` });
  });
  return { hook, sock };
}

/**
 * The server cancels every run started over a socket when that socket
 * closes, and a tab going away closes it. A reflexive reload must not throw
 * away a run in flight without asking.
 */
describe("leave-page guard while a run is live", () => {
  beforeEach(() => {
    FakeSocket.last = null;
  });
  afterEach(cleanup);

  it("lets an idle page leave silently", () => {
    renderHook(() => useConduit());
    expect(leaveIsBlocked()).toBe(false);
  });

  it("asks before leaving once a run has started", async () => {
    await started();
    expect(leaveIsBlocked()).toBe(true);
  });

  it("stops asking once the run completes", async () => {
    const { sock } = await started();
    act(() => {
      sock.onMessage({ type: "flow_complete", flowId: "f1" });
    });
    expect(leaveIsBlocked()).toBe(false);
  });

  it("stops asking once the run fails", async () => {
    const { sock } = await started();
    act(() => {
      sock.onMessage({ type: "flow_failed", flowId: "f1", error: "boom" });
    });
    expect(leaveIsBlocked()).toBe(false);
  });

  it("stops asking once the user cancels the run", async () => {
    const { hook } = await started();
    act(() => {
      hook.result.current.cancel("f1");
    });
    expect(leaveIsBlocked()).toBe(false);
  });

  it("keeps asking while one of two runs is still going", async () => {
    const { sock } = await started(2);
    act(() => {
      sock.onMessage({ type: "flow_complete", flowId: "f1" });
    });
    expect(leaveIsBlocked()).toBe(true);
    act(() => {
      sock.onMessage({ type: "flow_complete", flowId: "f2" });
    });
    expect(leaveIsBlocked()).toBe(false);
  });

  it("leaves no guard behind when the screen unmounts", async () => {
    const { hook } = await started();
    hook.unmount();
    expect(leaveIsBlocked()).toBe(false);
  });
});
