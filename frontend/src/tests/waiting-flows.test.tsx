import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { waitingFlowIds, type LiveRun } from "@/hooks/useConduit";
import { FlowHistory } from "@/features/dashboard/components/flow-history";

vi.mock("@/services/conduits", () => ({
  fetchFlows: () => Promise.resolve([]),
  fetchFlowLogs: () => Promise.resolve({ logs: [], tasks: [], runPath: undefined, children: [] }),
}));

// Radix's ScrollArea observes its viewport; jsdom ships no ResizeObserver.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

function run(over: Partial<LiveRun> & Pick<LiveRun, "flowId">): LiveRun {
  return {
    conduitName: "goal_loop",
    startedAt: 0,
    status: "running",
    logLines: [],
    taskStatuses: {},
    agentRequests: [],
    runPath: "",
    inputs: {},
    ...over,
  };
}

const ask = (flowId: string) => [{ flowId, requestId: "r1", prompt: "Which branch?" }];
const gate = { fromTool: "tool:hitl" as const, comment: "approve?" };

/**
 * A parked agent costs wall-clock time until someone answers, so a run waiting
 * on a person must never look like one that is merely busy.
 */
describe("waitingFlowIds", () => {
  it("flags a run with a HITL gate or an agent question pending", () => {
    expect(waitingFlowIds([run({ flowId: "a", hitlRequest: gate })])).toEqual(new Set(["a"]));
    expect(waitingFlowIds([run({ flowId: "a", agentRequests: ask("a") })])).toEqual(new Set(["a"]));
    expect(waitingFlowIds([run({ flowId: "a" })])).toEqual(new Set());
  });

  it("rolls a nested child's question up to the top-level run, however deep", () => {
    const runs = [
      run({ flowId: "p" }),
      run({ flowId: "c", parentFlowId: "p" }),
      run({ flowId: "g", parentFlowId: "c", agentRequests: ask("g") }),
    ];
    expect(waitingFlowIds(runs)).toEqual(new Set(["p"]));
  });

  it("ignores requests left on a run that is no longer running", () => {
    expect(waitingFlowIds([run({ flowId: "a", status: "cancelled", hitlRequest: gate })])).toEqual(new Set());
    expect(
      waitingFlowIds([
        run({ flowId: "p", status: "cancelled" }),
        run({ flowId: "c", parentFlowId: "p", agentRequests: ask("c") }),
      ]),
    ).toEqual(new Set());
  });
});

describe("recent flows list", () => {
  afterEach(cleanup);

  it("says waiting, not live, for a run parked on a question, in the row and in the drawer", async () => {
    render(<FlowHistory liveRuns={[run({ flowId: "a", agentRequests: ask("a") })]} />);
    const row = await screen.findByTestId("hist-row-running");
    expect(row.textContent).toContain("waiting");
    expect(row.textContent).not.toContain("live");
    fireEvent.click(row);
    expect(screen.getByTestId("flow-drawer-badge").textContent).toBe("waiting");
  });

  it("still says live for a run that is simply busy", async () => {
    render(<FlowHistory liveRuns={[run({ flowId: "a" })]} />);
    const row = await screen.findByTestId("hist-row-running");
    expect(row.textContent).toContain("live");
    expect(row.textContent).not.toContain("waiting");
  });
});
