import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import RunPage from "@/pages/Run";
import type { RunFeedHandlers } from "@/services/api/runs";
import type { RunSummary, RunTask, RunView } from "@/types/run";

const feeds: { flowId: string; handlers: RunFeedHandlers }[] = [];
let onOpen: (feed: { flowId: string; handlers: RunFeedHandlers }) => void = () => {};

// Each summary request waits until the test answers it, so a test controls
// which answer arrives first.
interface Pending {
  flowId: string;
  resolve: (s: RunSummary) => void;
  reject: (e: Error) => void;
}
const asked: Pending[] = [];

vi.mock("@/services/api/runs", () => ({
  fetchRunSummary: (flowId: string) =>
    new Promise<RunSummary>((resolve, reject) => asked.push({ flowId, resolve, reject })),
  openRunFeed: (flowId: string, handlers: RunFeedHandlers) => {
    const feed = { flowId, handlers };
    feeds.push(feed);
    queueMicrotask(() => onOpen(feed));
    return { watch: () => {}, close: () => {} };
  },
}));

const toast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

function task(name: string, status: string): RunTask {
  return { name, tool: "tool:bash", description: "", dependsOn: [], status, iteration: 1, of: 1 };
}

const FAILED_ID = "20261021_c8545b37_two";
const OTHER_ID = "20261021_187441ee_two";

function view(flowId: string, status: string, finish: string): RunView {
  return {
    flowId,
    conduitName: "two",
    status,
    startedAt: "2026-10-21T10:00:00Z",
    finishedAt: status === "running" ? null : "2026-10-21T10:00:01Z",
    runPath: null,
    currentTasks: status === "running" ? ["finish"] : [],
    tasks: [task("prepare", "completed"), task("finish", finish)],
  };
}

const RESUME = "cd '/work/my project' && atelier run --resume 20261021_c8545b37_two";

function summary(over: Partial<RunSummary>): RunSummary {
  return {
    flowId: FAILED_ID,
    status: "failed",
    observedState: "failed",
    observedCertainty: "proven",
    observedNote: "",
    settled: true,
    settledNote: null,
    final: false,
    workspace: "/work/my project",
    store: "/work/my project/.atelier",
    storeNote: null,
    parentFlowId: null,
    parentTask: null,
    results: [
      { task: "prepare", status: "completed", state: "text", text: "PREPARED\n", chars: 9, truncated: false },
    ],
    resultsUnavailable: null,
    fullResultsCommand: "cd '/work/my project' && atelier outputs 20261021_c8545b37_two",
    failures: [
      { task: "finish", kind: "task", exitCode: 3, reason: null, channel: "stderr", excerpt: "missing ready file", truncated: false },
    ],
    unavailable: [],
    nextSteps: [
      {
        what: "continue the run, keeping what already completed",
        command: "atelier run --resume 20261021_c8545b37_two",
        copyText: RESUME,
        sideEffects: "runs the tasks this run did not complete",
      },
    ],
    ...over,
  };
}

const COMPLETED = summary({
  status: "completed",
  observedState: "completed",
  final: true,
  failures: [],
  results: [
    { task: "prepare", status: "completed", state: "text", text: "PREPARED\n", chars: 9, truncated: false },
    { task: "finish", status: "completed", state: "text", text: "FINISHED\n", chars: 9, truncated: false },
  ],
  nextSteps: [{ what: "this run finished; there is nothing to recover", command: null, copyText: null, sideEffects: null }],
});

let navigate: (to: string) => void = () => {};
function Nav() {
  navigate = useNavigate();
  return null;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Nav />
      <Routes>
        <Route path="/runs/:flowId" element={<RunPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function answer(i: number, s: RunSummary) {
  await act(async () => asked[i].resolve(s));
}

beforeEach(() => {
  feeds.length = 0;
  asked.length = 0;
  toast.error.mockReset();
  onOpen = (feed) => feed.handlers.onFlow(view(feed.flowId, "failed", "failed"));
});

afterEach(() => cleanup());

describe("run page results and next step", () => {
  it("names the failure, keeps the saved result and gives the exact command", async () => {
    renderAt(`/runs/${FAILED_ID}`);
    await screen.findByTestId("run-summary-loading");
    await answer(0, summary({}));

    expect(screen.getByTestId("run-failures").textContent).toContain("missing ready file");
    expect(screen.getByTestId("run-failures").textContent).toContain("exit 3");
    expect(screen.getByTestId("run-result-prepare").textContent).toContain("PREPARED");
    expect(screen.getByText("Saved so far")).toBeTruthy();
    expect(screen.getByText(RESUME)).toBeTruthy();
    expect(screen.getByText(/project folder \/work\/my project/)).toBeTruthy();
    // The map and log stay below it.
    expect(screen.getByTestId("run-map-node-finish")).toBeTruthy();
  });

  it("refetches when the run moves on, and drops the old advice at once", async () => {
    renderAt(`/runs/${FAILED_ID}`);
    await screen.findByTestId("run-summary-loading");
    await answer(0, summary({}));
    expect(screen.getByText(RESUME)).toBeTruthy();

    act(() => feeds[0].handlers.onFlow(view(FAILED_ID, "running", "running")));
    expect(screen.queryByText(RESUME)).toBeNull();
    expect(asked).toHaveLength(2);

    act(() => feeds[0].handlers.onFlow(view(FAILED_ID, "completed", "completed")));
    expect(asked).toHaveLength(3);
    // The answer for the running state arrives late: it must not show.
    await answer(1, summary({ status: "running", observedState: "running", failures: [] }));
    expect(screen.queryByTestId("run-summary")).toBeNull();
    await answer(2, COMPLETED);
    expect(screen.getByText("Result")).toBeTruthy();
    expect(screen.getByTestId("run-result-finish").textContent).toContain("FINISHED");
    expect(screen.queryByTestId("run-failures")).toBeNull();
  });

  it("does not refetch for a log line", async () => {
    renderAt(`/runs/${FAILED_ID}`);
    await screen.findByTestId("run-summary-loading");
    act(() => feeds[0].handlers.onFlow(view(FAILED_ID, "failed", "failed")));
    expect(asked).toHaveLength(1);
  });

  it("never shows one run's summary on another run's page", async () => {
    renderAt(`/runs/${FAILED_ID}`);
    await screen.findByTestId("run-summary-loading");
    onOpen = () => {};
    act(() => navigate(`/runs/${OTHER_ID}`));
    await answer(0, summary({}));
    expect(screen.queryByText(RESUME)).toBeNull();
    expect(screen.queryByTestId("run-summary")).toBeNull();
  });

  it("keeps the map and offers a retry when the summary cannot be read", async () => {
    renderAt(`/runs/${FAILED_ID}`);
    await screen.findByTestId("run-summary-loading");
    await act(async () => asked[0].reject(new Error("API error 500: boom")));

    expect(screen.getByTestId("run-summary-error").textContent).toContain("boom");
    expect(screen.getByTestId("run-map-node-finish")).toBeTruthy();
    fireEvent.click(screen.getByText("Try again"));
    expect(asked).toHaveLength(2);
    await answer(1, summary({}));
    expect(screen.getByText(RESUME)).toBeTruthy();
  });

  it("tells the truth about each kind of saved result", async () => {
    renderAt(`/runs/${FAILED_ID}`);
    await screen.findByTestId("run-summary-loading");
    await answer(
      0,
      summary({
        results: [
          { task: "prepare", status: "completed", state: "text", text: "<b>x</b>", chars: 50000, truncated: true },
          { task: "finish", status: "completed", state: "empty", text: null, chars: 0, truncated: false },
        ],
      }),
    );
    // Shown as text, never as markup.
    expect(screen.getByText("<b>x</b>")).toBeTruthy();
    expect(screen.getByText(/first 8 of 50000 characters/)).toBeTruthy();
    expect(screen.getByText(/atelier outputs 20261021_c8545b37_two/)).toBeTruthy();
    expect(screen.getByText("Saved an empty result.")).toBeTruthy();
  });

  it("says so when copying is refused, without claiming it copied", async () => {
    Object.assign(navigator, { clipboard: { writeText: () => Promise.reject(new Error("no")) } });
    renderAt(`/runs/${FAILED_ID}`);
    await screen.findByTestId("run-summary-loading");
    await answer(0, summary({}));

    await act(async () => fireEvent.click(screen.getByTestId("copy-step-0")));
    expect(toast.error).toHaveBeenCalled();
    expect(screen.getByTestId("copy-step-0").textContent).toBe("copy");
  });

  it("explains where a missing run was looked for", async () => {
    onOpen = (feed) => feed.handlers.onError("flow not found: 20260101_deadbeef_two");
    renderAt("/runs/20260101_deadbeef_two");
    await screen.findByText(/flow not found/);
    await act(async () =>
      asked[0].reject(
        new Error("API error 404: No run 20260101_deadbeef_two in /work/my project. List it with `atelier list flows`."),
      ),
    );
    expect(screen.getByTestId("run-missing").textContent).toContain(
      "No run 20260101_deadbeef_two in /work/my project",
    );
    expect(screen.getByTestId("run-missing").textContent).not.toContain("API error");
  });
});
