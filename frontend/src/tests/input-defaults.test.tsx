import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act, renderHook } from "@testing-library/react";
import { inputDefault, inputHint, submittedInputs, type Conduit } from "@/types/conduit";
import { InputForm } from "@/features/dashboard/components/input-form";
import { ScheduleDialog } from "@/features/dashboard/components/schedule-dialog";
import { useNewTaskDialog } from "@/features/kanban/components/useNewTaskDialog";
import { useTaskStore } from "@/runner";

// `ticket` is required; `branch` has a default and so is optional. The engine
// fills `main` in when `branch` is left out, and an empty string sent in its
// place would override that default.
const conduit: Conduit = {
  name: "goal_loop",
  description: "",
  runPath: "/srv/app",
  inputs: {
    ticket: "the ticket to work on",
    branch: { description: "branch to start from", default: "main" },
  },
  tasks: [],
};

// One stable object: the kanban hook's reset effect depends on `conduits`,
// and a fresh array per render would re-run it on every render, forever.
const CTX = { conduits: [conduit], loading: false, error: null, refresh: () => {} };

vi.mock("@/services/ConduitProvider", () => ({
  useConduits: () => CTX,
  getConduitSync: (name: string, list: Conduit[]) => list.find((c) => c.name === name),
}));

describe("input helpers", () => {
  it("reads the default only from the object form", () => {
    expect(inputDefault("a ticket")).toBeUndefined();
    expect(inputDefault({ description: "d" })).toBeUndefined();
    expect(inputDefault({ description: "d", default: null })).toBeUndefined();
    expect(inputDefault({ description: "d", default: "main" })).toBe("main");
    expect(inputHint("a ticket")).toBe("a ticket");
    expect(inputHint({ description: "d", default: "main" })).toBe("d · default: main");
    expect(inputHint({ description: "", default: "main" })).toBe("default: main");
  });

  it("sends typed values, keeps required empties, drops an empty defaulted input", () => {
    expect(submittedInputs(conduit, { ticket: "ABC-1", branch: "" })).toEqual({ ticket: "ABC-1" });
    expect(submittedInputs(conduit, { ticket: "ABC-1", branch: "dev" })).toEqual({
      ticket: "ABC-1",
      branch: "dev",
    });
    expect(submittedInputs(conduit, {})).toEqual({ ticket: "" });
    // A value for an input the conduit no longer declares is not sent.
    expect(submittedInputs(conduit, { ticket: "x", stale: "y" })).toEqual({ ticket: "x" });
  });
});

describe("run form", () => {
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("shows the default as the placeholder and in the hint", () => {
    render(<InputForm conduit={conduit} onRun={vi.fn()} />);
    const branch = screen.getByLabelText("branch") as HTMLInputElement;
    expect(branch.placeholder).toBe("main");
    expect(branch.value).toBe("");
    expect(screen.getByText("branch to start from · default: main")).toBeTruthy();
    const ticket = screen.getByLabelText("ticket") as HTMLInputElement;
    expect(ticket.placeholder).toBe("the ticket to work on");
  });

  it("requires only the inputs without a default", () => {
    const onRun = vi.fn();
    render(<InputForm conduit={conduit} onRun={onRun} />);
    fireEvent.click(screen.getByTestId("run-conduit"));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getAllByText("Required")).toHaveLength(1);
    expect(screen.getByLabelText("ticket").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByLabelText("branch").getAttribute("aria-invalid")).toBe("false");
  });

  it("sends a typed value and leaves an empty defaulted input to the engine", () => {
    const onRun = vi.fn();
    render(<InputForm conduit={conduit} onRun={onRun} />);
    fireEvent.change(screen.getByLabelText("ticket"), { target: { value: "ABC-1" } });
    fireEvent.click(screen.getByTestId("run-conduit"));
    expect(onRun).toHaveBeenCalledWith({ ticket: "ABC-1" }, "/srv/app");

    fireEvent.change(screen.getByLabelText("branch"), { target: { value: "dev" } });
    fireEvent.click(screen.getByTestId("run-conduit"));
    expect(onRun).toHaveBeenLastCalledWith({ ticket: "ABC-1", branch: "dev" }, "/srv/app");
  });
});

describe("schedule dialog", () => {
  afterEach(cleanup);

  it("shows the default and leaves an empty defaulted input out of the schedule", () => {
    const onConfirm = vi.fn();
    render(
      <ScheduleDialog
        conduitName="goal_loop"
        defaultInputs={{ ticket: "ABC-1", branch: "" }}
        defaultRunPath="/srv/app"
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
      />,
    );
    const branch = screen.getByPlaceholderText("main") as HTMLInputElement;
    expect(branch.value).toBe("");
    expect(screen.getByText("branch to start from · default: main")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("goal_loop"), { target: { value: "nightly" } });
    fireEvent.click(screen.getByRole("button", { name: "schedule" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    const [, name, inputs, runPath] = onConfirm.mock.calls[0];
    expect(name).toBe("goal_loop");
    expect(inputs).toEqual({ ticket: "ABC-1" });
    expect(runPath).toBe("/srv/app");
  });
});

describe("kanban new-task dialog", () => {
  afterEach(() => useTaskStore.getState().setTasks([]));

  it("stores only the typed inputs, so the run it starts gets the default", () => {
    const { result } = renderHook(() =>
      useNewTaskDialog({ open: true, onOpenChange: vi.fn(), projectId: "p1" }),
    );
    act(() => result.current.selectConduitAndAdvance("goal_loop"));
    act(() => result.current.setValues((v) => ({ ...v, ticket: "ABC-1" })));
    act(() => result.current.submitConduit());
    expect(useTaskStore.getState().tasks).toHaveLength(1);
    expect(useTaskStore.getState().tasks[0].inputs).toEqual({ ticket: "ABC-1" });
  });
});
