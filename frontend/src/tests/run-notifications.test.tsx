import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, cleanup } from "@testing-library/react";
import { useRunNotifications } from "@/hooks/useRunNotifications";
import { NOTIFY_STORAGE_KEY } from "@/constants/dashboard";
import type { LiveRun } from "@/hooks/useConduit";

// jsdom has no Notification API; this stands in for it and records every notice.
class FakeNotification {
  static permission: NotificationPermission = "granted";
  static shown: FakeNotification[] = [];
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(
    public title: string,
    public options?: NotificationOptions,
  ) {
    FakeNotification.shown.push(this);
  }
}

function run(over: Partial<LiveRun> & Pick<LiveRun, "flowId" | "status">): LiveRun {
  return {
    conduitName: "goal_loop",
    startedAt: 1_000,
    logLines: [],
    taskStatuses: {},
    agentRequests: [],
    runPath: "/srv/app",
    inputs: {},
    ...over,
  };
}

const done = (flowId: string) =>
  run({ flowId, status: "done", logLines: [{ t: 6_000, text: "✓ flow complete", level: "ok" }] });
const asking = (flowId: string, prompt = "Which branch?") =>
  run({ flowId, status: "running", agentRequests: [{ flowId, requestId: "r1", prompt }] });

/** Mount the hook on the first snapshot, then feed it each later one. */
function play(first: LiveRun[], ...rest: LiveRun[][]) {
  const h = renderHook((runs: LiveRun[]) => useRunNotifications(runs), { initialProps: first });
  for (const runs of rest) h.rerender(runs);
  return h;
}

const titles = () => FakeNotification.shown.map((n) => n.title);

/** Put the page in or out of view: a focused, visible document is "in view". */
function pageInView(inView: boolean) {
  vi.spyOn(document, "hasFocus").mockReturnValue(inView);
  Object.defineProperty(document, "visibilityState", {
    value: inView ? "visible" : "hidden",
    configurable: true,
  });
}

/**
 * The tab title covers a glance at the tab strip; this covers the browser
 * being behind the editor. It fires only for a change of state, only for
 * runs the person would act on, and only once they opted in.
 */
describe("useRunNotifications", () => {
  beforeEach(() => {
    vi.stubGlobal("Notification", FakeNotification);
    FakeNotification.permission = "granted";
    FakeNotification.shown = [];
    localStorage.setItem(NOTIFY_STORAGE_KEY, "1");
    pageInView(false);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("says a run finished, and how long it took", () => {
    play([run({ flowId: "a", status: "running" })], [done("a")]);
    expect(FakeNotification.shown).toHaveLength(1);
    expect(FakeNotification.shown[0].title).toBe("✓ done · goal_loop");
    expect(FakeNotification.shown[0].options).toEqual({ body: "in 5.0s", tag: "a" });
  });

  it("says a run failed, with the engine's error line", () => {
    play(
      [run({ flowId: "a", status: "running" })],
      [run({ flowId: "a", status: "failed", logLines: [{ t: 2_000, text: "✗ task 'lint' failed: exit 1", level: "err" }] })],
    );
    expect(titles()).toEqual(["✗ failed · goal_loop"]);
    expect(FakeNotification.shown[0].options?.body).toBe("✗ task 'lint' failed: exit 1");
  });

  it("says a run is waiting, with the question", () => {
    play([run({ flowId: "a", status: "running" })], [asking("a")]);
    expect(titles()).toEqual(["? waiting · goal_loop"]);
    expect(FakeNotification.shown[0].options?.body).toBe("Which branch?");
  });

  it("uses a HITL gate's prompt as the question", () => {
    play(
      [run({ flowId: "a", status: "running" })],
      [run({ flowId: "a", status: "running", hitlRequest: { fromTool: "tool:hitl", comment: "Approve the plan?" } })],
    );
    expect(FakeNotification.shown[0].options?.body).toBe("Approve the plan?");
  });

  it("announces the parent when a nested child run asks, with the child's question", () => {
    const parent = run({ flowId: "p", status: "running" });
    const child = run({ flowId: "c", status: "running", conduitName: "inner", parentFlowId: "p", parentTask: "nested" });
    play(
      [parent, child],
      [parent, { ...child, agentRequests: [{ flowId: "c", requestId: "r1", prompt: "Overwrite?" }] }],
    );
    expect(titles()).toEqual(["? waiting · goal_loop"]);
    expect(FakeNotification.shown[0].options?.body).toBe("Overwrite?");
  });

  it("announces a run that waits a second time, and then its end", () => {
    play(
      [run({ flowId: "a", status: "running" })],
      [asking("a", "first?")],
      [run({ flowId: "a", status: "running" })],
      [asking("a", "second?")],
      [done("a")],
    );
    expect(titles()).toEqual(["? waiting · goal_loop", "? waiting · goal_loop", "✓ done · goal_loop"]);
    expect(FakeNotification.shown[1].options?.body).toBe("second?");
  });

  it("announces a run first seen already waiting", () => {
    // A started envelope and a request in the same render: the transition
    // still happened.
    play([], [asking("a")]);
    expect(titles()).toEqual(["? waiting · goal_loop"]);
  });

  it("stays quiet on a cancel, for child runs, and for a snapshot with no change", () => {
    play(
      [run({ flowId: "a", status: "running" })],
      [run({ flowId: "a", status: "cancelled" })],
      [run({ flowId: "a", status: "cancelled" })],
    );
    play(
      [run({ flowId: "c", status: "running", parentFlowId: "p" })],
      [run({ flowId: "c", status: "done", parentFlowId: "p" })],
    );
    play([run({ flowId: "a", status: "running" })], [done("a")], [done("a")]);
    expect(titles()).toEqual(["✓ done · goal_loop"]);
  });

  it("stays quiet while the page is in view", () => {
    pageInView(true);
    play([run({ flowId: "a", status: "running" })], [done("a")]);
    expect(FakeNotification.shown).toHaveLength(0);
  });

  it("fires when the tab is visible but its window is not focused", () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    play([run({ flowId: "a", status: "running" })], [done("a")]);
    expect(FakeNotification.shown).toHaveLength(1);
  });

  it("stays quiet unless the person opted in and the browser allows it", () => {
    localStorage.removeItem(NOTIFY_STORAGE_KEY);
    play([run({ flowId: "a", status: "running" })], [done("a")]);
    localStorage.setItem(NOTIFY_STORAGE_KEY, "1");
    FakeNotification.permission = "denied";
    play([run({ flowId: "b", status: "running" })], [done("b")]);
    vi.stubGlobal("Notification", undefined);
    play([run({ flowId: "c", status: "running" })], [done("c")]);
    expect(FakeNotification.shown).toHaveLength(0);
  });

  it("brings the tab to the front when clicked", () => {
    const focus = vi.spyOn(window, "focus").mockImplementation(() => {});
    play([run({ flowId: "a", status: "running" })], [done("a")]);
    FakeNotification.shown[0].onclick?.();
    expect(focus).toHaveBeenCalledTimes(1);
    expect(FakeNotification.shown[0].close).toHaveBeenCalledTimes(1);
  });

  it("survives a browser that refuses page notifications", () => {
    vi.stubGlobal(
      "Notification",
      class {
        static permission: NotificationPermission = "granted";
        constructor() {
          throw new TypeError("Illegal constructor. Use ServiceWorkerRegistration.showNotification() instead.");
        }
      },
    );
    expect(() => play([run({ flowId: "a", status: "running" })], [done("a")])).not.toThrow();
  });
});
