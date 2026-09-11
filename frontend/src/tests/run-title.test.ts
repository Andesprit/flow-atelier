import { describe, it, expect, afterEach } from "vitest";
import { renderHook, cleanup } from "@testing-library/react";
import { runTitle, useRunTitle, BASE_TITLE } from "@/hooks/useRunTitle";
import type { LiveRun } from "@/hooks/useConduit";

function run(over: Partial<LiveRun> & Pick<LiveRun, "flowId" | "status">): LiveRun {
  return {
    conduitName: "goal_loop",
    startedAt: 0,
    logLines: [],
    taskStatuses: {},
    agentRequests: [],
    runPath: "",
    inputs: {},
    ...over,
  };
}

/**
 * The title is read from another window, so it has to say the state before
 * anything else and must never be driven by a nested child flow.
 */
describe("runTitle", () => {
  it("is the plain app name with no runs", () => {
    expect(runTitle([])).toBe(BASE_TITLE);
  });

  it("names the running conduit", () => {
    expect(runTitle([run({ flowId: "a", status: "running" })])).toBe("● running · goal_loop");
  });

  it("counts several runs in flight", () => {
    expect(
      runTitle([
        run({ flowId: "a", status: "running" }),
        run({ flowId: "b", status: "running", conduitName: "other" }),
      ]),
    ).toBe(`● 2 running · ${BASE_TITLE}`);
  });

  it("shows the outcome of the most recently started run once nothing is running", () => {
    expect(
      runTitle([
        run({ flowId: "a", status: "failed", conduitName: "older" }),
        run({ flowId: "b", status: "done" }),
      ]),
    ).toBe("✓ done · goal_loop");
    expect(runTitle([run({ flowId: "a", status: "failed" })])).toBe("✗ failed · goal_loop");
    expect(runTitle([run({ flowId: "a", status: "cancelled" })])).toBe("■ cancelled · goal_loop");
  });

  it("ignores child flows of a nested conduit", () => {
    expect(runTitle([run({ flowId: "c", status: "running", parentFlowId: "p" })])).toBe(BASE_TITLE);
    expect(
      runTitle([
        run({ flowId: "p", status: "done" }),
        run({ flowId: "c", status: "failed", parentFlowId: "p", conduitName: "child" }),
      ]),
    ).toBe("✓ done · goal_loop");
  });
});

describe("useRunTitle", () => {
  afterEach(() => {
    cleanup();
    document.title = "";
  });

  it("writes the title, follows the run, and restores the app name on unmount", () => {
    const { rerender, unmount } = renderHook(({ runs }: { runs: LiveRun[] }) => useRunTitle(runs), {
      initialProps: { runs: [run({ flowId: "a", status: "running" })] },
    });
    expect(document.title).toBe("● running · goal_loop");

    rerender({ runs: [run({ flowId: "a", status: "done" })] });
    expect(document.title).toBe("✓ done · goal_loop");

    unmount();
    expect(document.title).toBe(BASE_TITLE);
  });
});
