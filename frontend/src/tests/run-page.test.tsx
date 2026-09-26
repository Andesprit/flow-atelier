import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import RunPage, { defaultTask } from "@/pages/Run";
import { applyTaskLogUpdate } from "@/features/run/feed";
import { hereBox, layoutRunMap, NODE_W } from "@/features/run/layout";
import type { RunFeedHandlers } from "@/services/api/runs";
import type { RunTask, RunView, TaskLog } from "@/types/run";

interface FakeFeed {
  flowId: string;
  handlers: RunFeedHandlers;
  watched: string[];
}

const feeds: FakeFeed[] = [];
// What the fake server does when the page connects; each test may replace it.
let onOpen: (feed: FakeFeed) => void = () => {};

vi.mock("@/services/api/runs", () => ({
  openRunFeed: (flowId: string, handlers: RunFeedHandlers) => {
    const feed: FakeFeed = { flowId, handlers, watched: [] };
    feeds.push(feed);
    queueMicrotask(() => onOpen(feed));
    return {
      watch: (task: string) => {
        feed.watched.push(task);
        queueMicrotask(() => feed.handlers.onTaskLog(logFor(task)));
      },
      close: () => {},
    };
  },
}));

function task(over: Partial<RunTask> & Pick<RunTask, "name">): RunTask {
  return {
    tool: "tool:bash",
    description: "",
    dependsOn: [],
    status: "completed",
    iteration: 1,
    of: 1,
    ...over,
  };
}

const VIEW: RunView = {
  flowId: "fix_issue_ab12cd34_20260925",
  conduitName: "fix_issue",
  status: "completed",
  startedAt: "2026-09-25T14:02:00Z",
  finishedAt: "2026-09-25T14:08:12Z",
  runPath: "/work/payments-api",
  currentTasks: [],
  tasks: [
    task({ name: "implement", tool: "harness:codex" }),
    task({ name: "run_tests", dependsOn: ["implement"], iteration: 3, of: 5 }),
  ],
};

function logFor(name: string): TaskLog {
  if (name === "implement") {
    return {
      task: name,
      tool: "harness:codex",
      status: "completed",
      reason: null,
      of: 1,
      rounds: [
        {
          iteration: 1,
          status: "completed",
          startedAt: null,
          durationSeconds: 183,
          exitCode: 0,
          lines: [
            { at: null, kind: "edit", text: "payments/retry.py", level: "info" },
            { at: null, kind: "thought", text: "Cap after jitter.", level: "info" },
            { at: null, kind: "said", text: "Capped the delay.", level: "info" },
          ],
        },
      ],
    };
  }
  const round = (iteration: number, status: string, line: string) => ({
    iteration,
    status,
    startedAt: null,
    durationSeconds: 40,
    exitCode: status === "failed" ? 1 : 0,
    lines: [
      {
        at: null,
        kind: status === "failed" ? "err" : "out",
        text: line,
        level: (status === "failed" ? "error" : "info") as "error" | "info",
      },
    ],
  });
  return {
    task: name,
    tool: "tool:bash",
    status: "completed",
    reason: null,
    of: 5,
    rounds: [
      round(1, "failed", "test_backoff_caps FAILED"),
      round(2, "failed", "test_jitter FAILED"),
      round(3, "completed", "44 passed"),
    ],
  };
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/runs/:flowId" element={<RunPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  feeds.length = 0;
  onOpen = (feed) => feed.handlers.onFlow(VIEW);
});

afterEach(() => {
  cleanup();
});

describe("run page", () => {
  it("shows the log of the task you click on the map", async () => {
    renderAt(`/runs/${VIEW.flowId}?task=implement`);
    expect(await screen.findByText("payments/retry.py")).toBeTruthy();

    fireEvent.click(screen.getByTestId("run-map-node-run_tests"));

    await waitFor(() => expect(feeds[0].watched).toEqual(["implement", "run_tests"]));
    expect(await screen.findByText("44 passed")).toBeTruthy();
    expect(screen.queryByText("payments/retry.py")).toBeNull();
    expect(screen.getByTestId("run-map-node-run_tests").getAttribute("aria-pressed")).toBe("true");
  });

  it("opens only the last round until you open another", async () => {
    renderAt(`/runs/${VIEW.flowId}?task=run_tests`);
    expect(await screen.findByText("44 passed")).toBeTruthy();
    expect(screen.queryByText("test_backoff_caps FAILED")).toBeNull();

    fireEvent.click(screen.getByText("Round 1"));

    expect(screen.getByText("test_backoff_caps FAILED")).toBeTruthy();
  });

  it("hides thoughts until asked, and filters to problems", async () => {
    renderAt(`/runs/${VIEW.flowId}?task=implement`);
    expect(await screen.findByText("Capped the delay.")).toBeTruthy();
    expect(screen.queryByText("Cap after jitter.")).toBeNull();

    fireEvent.click(screen.getByText("Show them"));
    expect(screen.getByText("Cap after jitter.")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Problems only"));
    expect(screen.getByText("Nothing matches.")).toBeTruthy();
  });

  it("says why a run cannot be loaded", async () => {
    onOpen = (feed) => feed.handlers.onError("flow not found: nope");
    renderAt("/runs/nope");
    expect(await screen.findByText(/flow not found/)).toBeTruthy();
  });

  it("adds pushed lines without asking again", async () => {
    renderAt(`/runs/${VIEW.flowId}?task=implement`);
    expect(await screen.findByText("payments/retry.py")).toBeTruthy();

    act(() =>
      feeds[0].handlers.onTaskUpdate({
        task: "implement",
        status: "completed",
        reason: null,
        of: 1,
        rounds: [
          {
            iteration: 1,
            status: "completed",
            startedAt: null,
            durationSeconds: 190,
            exitCode: 0,
            append: [{ at: null, kind: "run", text: "make test -k retry", level: "info" }],
          },
        ],
      }),
    );

    expect(screen.getByText("make test -k retry")).toBeTruthy();
    expect(screen.getByText("payments/retry.py")).toBeTruthy();
    expect(feeds[0].watched).toEqual(["implement"]);
  });

  it("follows what is running when the link names no task", async () => {
    onOpen = (feed) => feed.handlers.onFlow({ ...VIEW, status: "running", currentTasks: ["run_tests"] });
    renderAt(`/runs/${VIEW.flowId}`);
    expect(await screen.findByText("44 passed")).toBeTruthy();
    expect(feeds[0].watched).toEqual(["run_tests"]);
  });
});

describe("applyTaskLogUpdate", () => {
  it("appends to a round, adds a new one in order, and takes the new state", () => {
    const log = logFor("run_tests");
    const next = applyTaskLogUpdate(
      { ...log, rounds: log.rounds.slice(0, 2), status: "running" },
      {
        task: "run_tests",
        status: "running",
        reason: null,
        of: 5,
        rounds: [
          {
            iteration: 2,
            status: "failed",
            startedAt: null,
            durationSeconds: 40,
            exitCode: 1,
            append: [{ at: null, kind: "out", text: "late line", level: "info" }],
          },
          { iteration: 3, status: "running", startedAt: null, durationSeconds: null, exitCode: null, append: [] },
        ],
      },
    );
    expect(next.rounds.map((r) => r.iteration)).toEqual([1, 2, 3]);
    expect(next.rounds[1].lines.map((l) => l.text)).toEqual(["test_jitter FAILED", "late line"]);
    expect(next.rounds[2].lines).toEqual([]);
  });
});

describe("defaultTask", () => {
  it("prefers what is running, then what failed, then the first task", () => {
    const tasks = [task({ name: "a" }), task({ name: "b", status: "failed" }), task({ name: "c" })];
    expect(defaultTask({ ...VIEW, tasks, currentTasks: ["c"] })).toBe("c");
    expect(defaultTask({ ...VIEW, tasks, currentTasks: [] })).toBe("b");
    expect(defaultTask({ ...VIEW, tasks: [task({ name: "a" })], currentTasks: [] })).toBe("a");
  });
});

describe("layoutRunMap", () => {
  it("puts each task one column right of its deepest dependency", () => {
    const layout = layoutRunMap([
      task({ name: "a" }),
      task({ name: "b", dependsOn: ["a"] }),
      task({ name: "c", dependsOn: ["a"] }),
      task({ name: "d", dependsOn: ["b", "c"] }),
    ]);
    const x = Object.fromEntries(layout.nodes.map((n) => [n.name, n.x]));
    expect(x.b).toBe(x.c);
    expect(x.b).toBeGreaterThan(x.a);
    expect(x.d).toBeGreaterThan(x.b);
    expect(layout.edges.map((e) => `${e.from}->${e.to}`)).toEqual(["a->b", "a->c", "b->d", "c->d"]);
    expect(layout.width).toBeGreaterThanOrEqual(x.d + NODE_W);
  });

  it("frames only the tasks running now", () => {
    const { nodes } = layoutRunMap([task({ name: "a" }), task({ name: "b", dependsOn: ["a"] })]);
    const box = hereBox(nodes, ["b"])!;
    const b = nodes.find((n) => n.name === "b")!;
    expect(box.x).toBeLessThan(b.x);
    expect(box.x + box.width).toBeGreaterThan(b.x + NODE_W);
    expect(hereBox(nodes, [])).toBeUndefined();
  });
});
