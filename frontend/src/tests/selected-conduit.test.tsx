import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Dashboard from "@/features/dashboard/Dashboard";
import { SELECTED_CONDUIT_STORAGE_KEY as KEY } from "@/constants/dashboard";
import type { Conduit } from "@/types/conduit";

const mk = (name: string): Conduit => ({ name, description: "", inputs: {}, tasks: [] });
const LOADED = { conduits: [mk("alpha"), mk("beta")], loading: false, error: null, refresh: () => {} };
const LOADING = { conduits: [], loading: true, error: null, refresh: () => {} };
let ctx = LOADED;

vi.mock("@/services/ConduitProvider", () => ({
  useConduits: () => ctx,
  getConduitSync: (name: string, list: Conduit[]) => list.find((c) => c.name === name),
}));

// The dashboard opens a socket and fetches flows and schedules on mount; none
// of that is under test here.
const HOOK = {
  run: vi.fn(),
  cancel: vi.fn(),
  resume: vi.fn(),
  answerHITL: vi.fn(),
  answerAgentInput: vi.fn(),
  liveRuns: [],
};
vi.mock("@/hooks/useConduit", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useConduit")>()),
  useConduit: () => HOOK,
}));
vi.mock("@/services/conduits", () => ({
  fetchFlows: () => Promise.resolve([]),
  fetchFlowLogs: () => Promise.resolve({ logs: [], tasks: [], runPath: undefined, children: [] }),
  fetchSchedules: () => Promise.resolve([]),
}));

// A fresh element each time: React skips a subtree handed the same element
// object, so rerendering a constant would not pick up the changed context.
const ui = () => (
  <MemoryRouter>
    <Dashboard />
  </MemoryRouter>
);

// Let the mount-time fetches resolve so their state updates land inside act.
const settle = () => act(() => Promise.resolve());

/** The one conduit row that is pressed. */
const activeRow = () => screen.getByRole("button", { pressed: true }).textContent ?? "";

/**
 * The run form already remembers each conduit's inputs and working directory;
 * the conduit itself was reset to the first one on every reload and every trip
 * to another screen.
 */
describe("dashboard remembers the selected conduit", () => {
  afterEach(() => {
    cleanup();
    localStorage.clear();
    ctx = LOADED;
  });

  it("restores the stored choice once the conduits have loaded", async () => {
    localStorage.setItem(KEY, "beta");
    ctx = LOADING;
    const { rerender } = render(ui());
    await settle();
    expect(screen.getByTestId("dashboard-loading")).toBeTruthy();
    ctx = LOADED;
    rerender(ui());
    expect(activeRow()).toContain("beta");
  });

  it("stores a new choice", async () => {
    render(ui());
    await settle();
    expect(activeRow()).toContain("alpha");
    fireEvent.click(screen.getByText("beta"));
    expect(activeRow()).toContain("beta");
    expect(localStorage.getItem(KEY)).toBe("beta");
  });

  it("falls back to the first conduit when the stored one is gone, and stores that", async () => {
    localStorage.setItem(KEY, "gone");
    render(ui());
    await settle();
    expect(activeRow()).toContain("alpha");
    expect(localStorage.getItem(KEY)).toBe("alpha");
  });

  it("selects the first conduit with nothing stored", async () => {
    render(ui());
    await settle();
    expect(activeRow()).toContain("alpha");
    expect(localStorage.getItem(KEY)).toBe("alpha");
  });
});
