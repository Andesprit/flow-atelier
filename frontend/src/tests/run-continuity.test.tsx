import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, Link, useLocation, useNavigate } from "react-router-dom";
import type { ClientWsMessage, ServerWsMessage } from "@/types/ws";
import type { Conduit } from "@/types/conduit";

// Radix's ScrollArea observes its viewport; jsdom ships no ResizeObserver.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// The wire is the only thing faked: every socket the app opens is recorded so
// the test can drive the server side and see what was sent and closed.
class FakeSocket {
  static all: FakeSocket[] = [];
  sent: ClientWsMessage[] = [];
  closed = false;
  onMessage: (msg: ServerWsMessage) => void = () => {};
  onClose: (code: number, reason: string) => void = () => {};
  readyState = 1;
  constructor() {
    FakeSocket.all.push(this);
  }
  send(msg: ClientWsMessage) {
    this.sent.push(msg);
  }
  close() {
    this.closed = true;
  }
  waitForOpen() {
    return Promise.resolve();
  }
}
vi.mock("@/services/api/run-conduit", () => ({ RunConduitSocket: FakeSocket }));

const SLOW: Conduit = {
  name: "slow",
  description: "",
  inputs: {},
  tasks: [
    { name: "gate", tool: "tool:hitl", description: "Ask", task: "go on?", dependsOn: [] },
    { name: "call", tool: "tool:conduit", description: "Nested", task: "inner", dependsOn: ["gate"] },
  ],
};
vi.mock("@/services/ConduitProvider", () => ({
  useConduits: () => ({ conduits: [SLOW, { ...SLOW, name: "late" }], loading: false, error: null, refresh: () => {} }),
  getConduitSync: (name: string, list: Conduit[]) => list.find((c) => c.name === name),
}));
const fetchFlows = vi.fn(() => Promise.resolve([]));
vi.mock("@/services/conduits", () => ({
  fetchFlows: () => fetchFlows(),
  fetchFlowLogs: () => Promise.resolve({ logs: [], tasks: [], runPath: undefined, children: [] }),
  fetchSchedules: () => Promise.resolve([]),
  getConduitCached: () => undefined,
}));

const { LiveRunsProvider, useKanbanRuns } = await import("@/context/LiveRunsContext");
const { TopBar } = await import("@/layout/NavBar");
const { ThemeProvider } = await import("@/context/ThemeContext");
const { default: Dashboard } = await import("@/features/dashboard/Dashboard");
const { Kanban } = await import("@/features/kanban/Kanban");
const { useTaskStore } = await import("@/runner");
const { saveRunPath } = await import("@/services/api/conduit-run-path");

let path = "";
let go: (delta: number) => void = () => {};
function Where() {
  path = useLocation().pathname;
  const navigate = useNavigate();
  go = (delta) => navigate(delta);
  return null;
}
// Stands in for Designer and the run page: any screen that is not the origin.
const Elsewhere = () => (
  <div>
    elsewhere <Link to="/runs/d1">run page</Link>
  </div>
);
// The board's own start path goes through the runner engine; this starts a
// run on the same kanban session the board uses.
let startOnBoard: (name: string) => void = () => {};
function BoardStarter() {
  const { run } = useKanbanRuns();
  startOnBoard = (name) => run(name, {}, "/p");
  return null;
}

const app = (at: string) => (
  <ThemeProvider>
  <MemoryRouter initialEntries={[at]}>
    <LiveRunsProvider>
      <TopBar />
      <Where />
      <BoardStarter />
      <Routes>
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/kanban" element={<Kanban />} />
        <Route path="/designer" element={<Elsewhere />} />
        <Route path="/runs/:flowId" element={<Elsewhere />} />
      </Routes>
    </LiveRunsProvider>
  </MemoryRouter>
  </ThemeProvider>
);

const settle = () => act(() => new Promise((r) => setTimeout(r, 0)));
const nav = (name: string) => fireEvent.click(screen.getByRole("link", { name: new RegExp(`^${name}$`, "i") }));
const serve = (sock: FakeSocket, ...msgs: object[]) =>
  act(() => msgs.forEach((m) => sock.onMessage(m as ServerWsMessage)));
const closeDrawer = () => fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
const runsSent = (sock: FakeSocket) => sock.sent.filter((m) => m.type === "run" || m.type === "resume");

beforeEach(() => {
  FakeSocket.all = [];
  localStorage.clear();
  saveRunPath("slow", "/p");
  fetchFlows.mockClear();
  useTaskStore.getState().setTasks([]);
});
afterEach(cleanup);

// Each case mounts the real dashboard and board; under a full parallel run on a
// loaded machine one case took just over the 5 s default.
describe("live runs outlive the screen they started on", { timeout: 30_000 }, () => {
  it("keeps a dashboard run, its gate and its nested question across screens", async () => {
    render(app("/dashboard"));
    await settle();
    fireEvent.click(screen.getByTestId("run-conduit"));
    await settle();
    const [sock] = FakeSocket.all;
    serve(sock, { type: "started", flowId: "d1" });
    expect(screen.getByTestId("flow-drawer")).toBeTruthy();
    closeDrawer();
    await settle();

    // Leave through the top bar, then on through a run link.
    nav("designer");
    await settle();
    fireEvent.click(screen.getByRole("link", { name: "run page" }));
    await settle();
    expect(path).toBe("/runs/d1");
    expect(sock.closed).toBe(false);
    expect(screen.getByTestId("active-work").textContent).toContain("1 running");

    // A gate on the run and a question from its nested child, while away.
    serve(
      sock,
      { type: "started", flowId: "c1", parentFlowId: "d1", parentTask: "call", conduitName: "inner" },
      { type: "hitl_request", flowId: "d1", task: "gate", inputs: [{ name: "ok", description: "go on?" }] },
      { type: "agent_input_request", flowId: "c1", requestId: "r-9", prompt: "which colour?", task: "ask" },
    );
    const chip = screen.getByTestId("active-work");
    expect(chip.textContent).toContain("1 waiting");
    expect(chip.getAttribute("aria-label")).toMatch(/slow is waiting on you \(dashboard\)/);
    expect(screen.queryByTestId("flow-drawer")).toBeNull();

    // Back to the controls through the chip; history is read fresh.
    const fetchesBefore = fetchFlows.mock.calls.length;
    fireEvent.click(chip);
    await settle();
    expect(path).toBe("/dashboard");
    expect(fetchFlows.mock.calls.length).toBeGreaterThan(fetchesBefore);
    const drawer = screen.getByTestId("flow-drawer");
    expect(within(drawer).getByTestId("flow-drawer-id").textContent).toContain("d1");

    const sentBefore = sock.sent.length;
    fireEvent.change(within(drawer).getByLabelText("ok"), { target: { value: "yes" } });
    fireEvent.submit(within(drawer).getByLabelText("ok").closest("form")!);
    fireEvent.change(within(drawer).getByTestId("agent-input-field"), { target: { value: "blue" } });
    fireEvent.click(within(drawer).getByTestId("agent-input-submit"));
    expect(sock.sent.slice(sentBefore)).toEqual([
      { type: "hitl_answer", flowId: "d1", answers: { ok: "yes" } },
      { type: "agent_input_answer", flowId: "c1", requestId: "r-9", answer: "blue" },
    ]);

    // Asking again for the same run, already on its screen, reopens a closed
    // drawer and sends nothing.
    closeDrawer();
    await settle();
    expect(screen.queryByTestId("flow-drawer")).toBeNull();
    const sentAfterAnswers = sock.sent.length;
    fireEvent.click(screen.getByTestId("active-work"));
    await settle();
    expect(screen.getByTestId("flow-drawer")).toBeTruthy();
    expect(sock.sent.length).toBe(sentAfterAnswers);

    serve(sock, { type: "flow_complete", flowId: "c1" }, { type: "flow_complete", flowId: "d1" });
    expect(screen.queryByTestId("active-work")).toBeNull();
    expect(FakeSocket.all).toHaveLength(1);
    expect(runsSent(sock)).toHaveLength(1);
  });

  it("does not pop a drawer for a start that lands while the dashboard is away", async () => {
    render(app("/dashboard"));
    await settle();
    fireEvent.click(screen.getByTestId("run-conduit"));
    await settle();
    nav("designer");
    await settle();
    const [sock] = FakeSocket.all;
    serve(sock, { type: "started", flowId: "d2" });
    nav("dashboard");
    await settle();
    expect(screen.queryByTestId("flow-drawer")).toBeNull();
    // The run is still live and reachable.
    expect(screen.getByTestId("active-work").getAttribute("aria-label")).toMatch(/slow is running/);
  });

  it("moves a kanban card to done when its run ends while the board is away, and returns to its own project", async () => {
    useTaskStore.getState().setTasks([
      { name: "other-card", projectId: "default", inputs: {}, createdAt: 1, column: "todo" },
      {
        name: "slow",
        projectId: "p2",
        inputs: {},
        createdAt: 2,
        column: "in_progress",
        flow: { startedAt: 2, currentTaskIndex: 0, taskStatuses: {}, logLines: [] },
      },
      {
        name: "late",
        projectId: "p2",
        inputs: {},
        createdAt: 3,
        column: "in_progress",
        flow: { startedAt: 3, currentTaskIndex: 0, taskStatuses: {}, logLines: [] },
      },
    ]);
    render(app("/kanban"));
    await settle();
    act(() => startOnBoard("slow"));
    act(() => startOnBoard("late"));
    await settle();
    const [kSock] = FakeSocket.all;
    nav("designer");
    await settle();
    expect(kSock.closed).toBe(false);

    serve(kSock, { type: "started", flowId: "k1" }, { type: "started", flowId: "k2" });
    expect(useTaskStore.getState().tasks.find((t) => t.name === "slow")?.flow?.flowId).toBe("k1");
    expect(useTaskStore.getState().tasks.find((t) => t.name === "late")?.flow?.flowId).toBe("k2");
    serve(kSock, { type: "flow_complete", flowId: "k1" });
    expect(useTaskStore.getState().tasks.find((t) => t.name === "slow")?.column).toBe("done");
    expect(useTaskStore.getState().tasks.find((t) => t.name === "late")?.column).toBe("in_progress");

    // Return to the run that is still going: the board shows its project, not
    // the remembered one, and opens that card.
    serve(kSock, { type: "hitl_request", flowId: "k2", task: "gate", inputs: [{ name: "ok" }] });
    fireEvent.click(screen.getByTestId("active-work"));
    await settle();
    expect(path).toBe("/kanban");
    expect(screen.getByRole("combobox", { name: "Filter by project", hidden: true }).textContent).not.toContain("my project");
    const drawer = screen.getByTestId("flow-drawer");
    expect(drawer.textContent).toContain("late");
    fireEvent.change(within(drawer).getByLabelText("ok"), { target: { value: "yes" } });
    fireEvent.submit(within(drawer).getByLabelText("ok").closest("form")!);
    expect(kSock.sent.at(-1)).toEqual({ type: "hitl_answer", flowId: "k2", answers: { ok: "yes" } });
    expect(runsSent(kSock)).toHaveLength(2);
  });

  it("reopens the run on Back and Forward without sending anything", async () => {
    render(app("/dashboard"));
    await settle();
    fireEvent.click(screen.getByTestId("run-conduit"));
    await settle();
    const [sock] = FakeSocket.all;
    serve(sock, { type: "started", flowId: "d1" });
    closeDrawer();
    nav("designer");
    await settle();
    fireEvent.click(screen.getByTestId("active-work"));
    await settle();
    expect(screen.getByTestId("flow-drawer")).toBeTruthy();
    closeDrawer();
    nav("designer");
    await settle();
    const sent = sock.sent.length;

    act(() => go(-1));
    await settle();
    expect(path).toBe("/dashboard");
    expect(screen.getByTestId("flow-drawer")).toBeTruthy();
    act(() => go(1));
    await settle();
    expect(path).toBe("/designer");
    expect(sock.sent.length).toBe(sent);
    expect(runsSent(sock)).toHaveLength(1);
    expect(sock.closed).toBe(false);
  });

  it("keeps two origins independent: cancelling one leaves the other running and guarded", async () => {
    useTaskStore.getState().setTasks([
      {
        name: "slow",
        projectId: "default",
        inputs: {},
        createdAt: 2,
        column: "in_progress",
        flow: { startedAt: 2, currentTaskIndex: 0, taskStatuses: {}, logLines: [] },
      },
    ]);
    const view = render(app("/dashboard"));
    await settle();
    fireEvent.click(screen.getByTestId("run-conduit"));
    act(() => startOnBoard("slow"));
    await settle();
    const [dSock, kSock] = FakeSocket.all;
    serve(dSock, { type: "started", flowId: "d1" });
    serve(kSock, { type: "started", flowId: "k1" });
    closeDrawer();
    nav("designer");
    await settle();

    // Both are listed, each pointing at its own screen.
    fireEvent.click(screen.getByTestId("active-work"));
    const list = await screen.findByRole("list", { name: "Runs going" });
    const links = within(list).getAllByRole("link");
    expect(links.map((l) => l.getAttribute("aria-label"))).toEqual([
      "slow is running (dashboard). Open its controls.",
      "slow is running (kanban). Open its controls.",
    ]);

    const leave = () => {
      const e = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(e);
      return e.defaultPrevented;
    };
    expect(leave()).toBe(true);

    // Cancel the dashboard run from its own drawer.
    fireEvent.click(links[0]);
    await settle();
    fireEvent.click(within(screen.getByTestId("flow-drawer")).getByRole("button", { name: /cancel/i }));
    const confirm = await screen.findByRole("dialog", { name: "Cancel run?" });
    fireEvent.click(within(confirm).getByRole("button", { name: /^cancel run$/i }));
    serve(dSock, { type: "flow_failed", flowId: "d1", error: "cancelled" });
    expect(dSock.sent.filter((m) => m.type === "cancel")).toEqual([{ type: "cancel", flowId: "d1" }]);
    expect(kSock.sent.some((m) => m.type === "cancel")).toBe(false);
    expect(leave()).toBe(true);
    expect(screen.getByTestId("active-work").getAttribute("aria-label")).toMatch(/\(kanban\)/);

    serve(kSock, { type: "flow_complete", flowId: "k1" });
    expect(leave()).toBe(false);
    expect(screen.queryByTestId("active-work")).toBeNull();

    // The tab going away still closes both sockets, so the server stops their work.
    view.unmount();
    expect(dSock.closed && kSock.closed).toBe(true);
  });
});
