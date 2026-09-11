import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { FlowDrawer } from "@/components/FlowDrawer";
import { FlowHistory } from "@/features/dashboard/components/flow-history";
import type { LiveRun } from "@/hooks/useConduit";

const priorFlow = {
  flowId: "20260910_0badcafe_goal_loop",
  conduitName: "goal_loop",
  startedAt: 1_000,
  duration: 5_000,
  status: "done" as const,
};

vi.mock("@/services/conduits", () => ({
  fetchFlows: () => Promise.resolve([priorFlow]),
  fetchFlowLogs: () =>
    Promise.resolve({ logs: [], tasks: [], runPath: "/srv/prior", children: [] }),
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
    status: "done",
    logLines: [],
    taskStatuses: {},
    agentRequests: [],
    runPath: "/srv/app",
    inputs: { ticket: "ABC-123", branch: "main" },
    ...over,
  };
}

/**
 * Two runs of one conduit look identical in the list, and rerunning a failed
 * one meant rebuilding the form by hand. The drawer is the record of the run:
 * what it was given, where it ran, and how to do it again.
 */
describe("FlowDrawer started-with block", () => {
  afterEach(cleanup);

  it("lists the working directory and every input, selectable", () => {
    render(
      <FlowDrawer
        open
        onClose={() => {}}
        title="goal_loop"
        runPath="/srv/app"
        inputs={{ ticket: "ABC-123", branch: "main" }}
      />,
    );
    const block = screen.getByTestId("flow-drawer-started-with");
    expect(block.textContent).toContain("working directory");
    expect(block.textContent).toContain("/srv/app");
    expect(block.textContent).toContain("ticket");
    expect(block.textContent).toContain("ABC-123");
    expect(block.textContent).toContain("branch");
    expect(block.textContent).toContain("main");
    expect(block.querySelectorAll(".select-all")).toHaveLength(3);
  });

  it("renders nothing when there is neither a path nor an input", () => {
    render(<FlowDrawer open onClose={() => {}} title="goal_loop" inputs={{}} runPath="" />);
    expect(screen.queryByTestId("flow-drawer-started-with")).toBeNull();
    expect(screen.queryByTestId("drawer-run-again-button")).toBeNull();
  });

  it("runs again and closes when asked", () => {
    const onRunAgain = vi.fn();
    const onClose = vi.fn();
    render(<FlowDrawer open onClose={onClose} title="goal_loop" onRunAgain={onRunAgain} />);
    fireEvent.click(screen.getByTestId("drawer-run-again-button"));
    expect(onRunAgain).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("recent flows list", () => {
  afterEach(cleanup);

  it("reruns a finished run with its own conduit, inputs and working directory", async () => {
    const onRunAgain = vi.fn();
    render(<FlowHistory liveRuns={[run({ flowId: "a", status: "failed" })]} onRunAgain={onRunAgain} />);
    fireEvent.click(await screen.findByTestId("hist-row"));
    const block = screen.getByTestId("flow-drawer-started-with");
    expect(block.textContent).toContain("/srv/app");
    expect(block.textContent).toContain("ABC-123");
    fireEvent.click(screen.getByTestId("drawer-run-again-button"));
    expect(onRunAgain).toHaveBeenCalledWith(
      "goal_loop",
      { ticket: "ABC-123", branch: "main" },
      "/srv/app",
    );
  });

  it("offers no rerun while the run is still going", async () => {
    render(<FlowHistory liveRuns={[run({ flowId: "a", status: "running" })]} onRunAgain={vi.fn()} />);
    fireEvent.click(await screen.findByTestId("hist-row-running"));
    expect(screen.getByTestId("flow-drawer-started-with").textContent).toContain("/srv/app");
    expect(screen.queryByTestId("drawer-run-again-button")).toBeNull();
  });

  it("offers no rerun for a run whose working directory it does not know", async () => {
    // A prior flow resumed in this session becomes a live run with no path.
    render(
      <FlowHistory
        liveRuns={[run({ flowId: "a", status: "failed", runPath: "", inputs: {} })]}
        onRunAgain={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByTestId("hist-row"));
    expect(screen.queryByTestId("flow-drawer-started-with")).toBeNull();
    expect(screen.queryByTestId("drawer-run-again-button")).toBeNull();
  });

  it("shows the directory the server recorded for a prior run, and no rerun", async () => {
    render(<FlowHistory liveRuns={[]} onRunAgain={vi.fn()} />);
    fireEvent.click(await screen.findByTestId("hist-row"));
    await waitFor(() =>
      expect(screen.getByTestId("flow-drawer-started-with").textContent).toContain("/srv/prior"),
    );
    expect(screen.queryByTestId("drawer-run-again-button")).toBeNull();
  });
});
