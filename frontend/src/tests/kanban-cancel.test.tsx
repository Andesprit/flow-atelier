import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { runningCardCancel } from "@/features/kanban/Kanban";
import { TaskCardRunning } from "@/features/kanban/components/TaskCardRunning";
import type { Task } from "@/types/task";
import type { LiveRun } from "@/hooks/useConduit";

const task = {
  name: "slow_sleep",
  column: "in_progress",
  flow: { flowId: "f1", taskStatuses: {}, logLines: [] },
} as unknown as Task;

const run = (status: string) => ({ flowId: "f1", status }) as unknown as LiveRun;

describe("cancel on a running Kanban card", () => {
  it("cancels a run the server hosts on the server", () => {
    const cancel = vi.fn();
    runningCardCancel(task, [run("running")], cancel)?.();
    expect(cancel).toHaveBeenCalledWith("f1");
  });

  it("offers no cancel once the hosted run has ended", () => {
    expect(runningCardCancel(task, [run("completed")], vi.fn())).toBeUndefined();
  });

  it("offers no cancel for a run the server cannot cancel", () => {
    expect(runningCardCancel(task, [], vi.fn())).toBeUndefined();
  });
});

vi.mock("@/services/ConduitProvider", () => ({
  useConduits: () => ({ conduits: [] }),
  getConduitSync: () => undefined,
}));

describe("TaskCardRunning", () => {
  it("shows no cancel button without a cancel action", () => {
    render(<TaskCardRunning task={task} />);
    expect(screen.queryByTestId("cancel-button")).toBeNull();
  });

  it("runs the cancel action it is given", () => {
    const onCancel = vi.fn();
    render(<TaskCardRunning task={task} onCancel={onCancel} />);
    fireEvent.click(screen.getByTestId("cancel-button"));
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
