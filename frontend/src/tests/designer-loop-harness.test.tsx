// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Conduit, ConduitTask } from "@/types/conduit";
import { Inspector } from "@/features/designer/components/Inspector";
import { TaskNode } from "@/features/designer/components/TaskNode";
import { fromWireTasks, toWireTasks } from "@/utils/conditions";
import { toCamelCase, toSnakeCase } from "@/services/transforms";
import { renderConduitYaml } from "@/utils/yaml";
import { ReactFlowProvider } from "@xyflow/react";

const body: ConduitTask[] = [
  { name: "fix", tool: "harness:claude-code:opus[1m]:high", description: "fix code", task: "Fix it", dependsOn: [] },
  { name: "test", tool: "tool:bash", description: "run tests", task: "pytest", dependsOn: ["fix"] },
];
const loop: ConduitTask = {
  name: "fix_until_green",
  tool: "tool:conduit",
  description: "fix until tests pass",
  task: "~inline~ship~fix_until_green",
  dependsOn: ["plan"],
  repeat: 4,
  until: "output.match(TESTS PASSED)",
  while: null,
  onExhaust: "fail",
  inputs: { goal: "{{inputs.goal}}" },
  tasks: body,
};
const conduit: Conduit = {
  name: "ship",
  description: "Plan, fix, review",
  inputs: { goal: "the change" },
  tasks: [loop],
};

afterEach(cleanup);

describe("designer loop and harness fields", () => {
  it("round-trips a starter-shaped API DTO through designer and wire, including inline body", () => {
    const wire = toSnakeCase({ ...conduit, tasks: toWireTasks(conduit.tasks) });
    const loaded = toCamelCase<Conduit>(wire);
    const designer = { ...loaded, tasks: fromWireTasks(loaded.tasks) };
    designer.tasks[0].position = { x: 10, y: 20 };
    designer.tasks[0].tasks![0].position = { x: 30, y: 40 };
    const saved = toSnakeCase({ ...designer, tasks: toWireTasks(designer.tasks) });

    expect(saved).toEqual(wire);
    const task = designer.tasks[0];
    expect([task.until, task.while, task.onExhaust, task.tasks?.[0].tool]).toEqual([
      "output.match(TESTS PASSED)", null, "fail", "harness:claude-code:opus[1m]:high",
    ]);
    const yaml = renderConduitYaml(designer);
    expect(yaml).toContain('until: "output.match(TESTS PASSED)"');
    expect(yaml).toContain("on_exhaust: fail");
    expect(yaml).toContain("      - name: fix");
    expect(yaml).toContain("tool: harness:claude-code:opus[1m]:high");
    expect(yaml).not.toContain("task: \"~inline~ship~fix_until_green\"");
  });

  it("edits stop mode and exhaustion rule and explains the inline body", () => {
    const update = vi.fn();
    render(<Inspector task={loop} conduit={conduit} conduits={[]} onUpdateTask={update} conduitInputs={conduit.inputs} onAddInput={vi.fn()} />);
    expect(screen.getByText(/harness:claude-code:opus\[1m\]:high/)).toBeTruthy();
    expect(screen.getByText(/--agent fix_until_green.TASK=HARNESS/)).toBeTruthy();
    const mode = screen.getByRole("combobox", { name: "stop when" });
    expect((mode as HTMLSelectElement).value).toBe("until");
    fireEvent.change(mode, { target: { value: "while" } });
    expect(update).toHaveBeenCalledWith("fix_until_green", {
      until: null, while: "output.match(TESTS PASSED)",
    });
    fireEvent.change(screen.getByRole("textbox", { name: "output condition" }), { target: { value: "output.not_match(READY)" } });
    expect(update).toHaveBeenCalledWith("fix_until_green", { while: "output.not_match(READY)" });
    fireEvent.click(screen.getByRole("checkbox", { name: /Fail the run/ }));
    expect(update).toHaveBeenCalledWith("fix_until_green", { onExhaust: "complete" });
    expect(screen.getByText(/dependent tasks run on the last result/)).toBeTruthy();
    fireEvent.change(mode, { target: { value: "none" } });
    expect(screen.getByRole("checkbox", { name: /Fail the run/ }).hasAttribute("disabled")).toBe(true);
  });

  it("lets a harness task choose a palette agent, free-text agent, model and effort", () => {
    const update = vi.fn();
    render(<Inspector task={body[0]} conduit={conduit} conduits={[]} onUpdateTask={update} conduitInputs={conduit.inputs} onAddInput={vi.fn()} />);
    const agent = screen.getByRole("combobox", { name: "agent" });
    expect((agent as HTMLInputElement).value).toBe("claude-code");
    expect((screen.getByRole("textbox", { name: "model" }) as HTMLInputElement).value).toBe("opus[1m]");
    fireEvent.change(agent, { target: { value: "my-agent" } });
    expect(update).toHaveBeenCalledWith("fix", { tool: "harness:my-agent:opus[1m]:high" });
    fireEvent.change(screen.getByRole("textbox", { name: "model" }), { target: { value: "model/v2" } });
    expect(update).toHaveBeenCalledWith("fix", { tool: "harness:my-agent:model/v2:high" });
    fireEvent.change(screen.getByRole("textbox", { name: "effort" }), { target: { value: "xhigh" } });
    expect(update).toHaveBeenCalledWith("fix", { tool: "harness:my-agent:model/v2:xhigh" });
  });

  it("shows the loop condition and amber exhaustion marker on the canvas node", () => {
    const props = { id: loop.name, data: { idx: 2, name: loop.name, tool: loop.tool, task: loop.task, description: loop.description, repeat: 4, until: loop.until, onExhaust: "complete" }, selected: false } as unknown as React.ComponentProps<typeof TaskNode>;
    render(<ReactFlowProvider><TaskNode {...props} /></ReactFlowProvider>);
    const badge = screen.getByLabelText(/↻4 until TESTS PASSED/);
    // Solid, so the node's top border does not show through the badge text.
    expect(badge.className).toContain("bg-card");
    expect(badge.className).not.toMatch(/bg-\S+\/\d+/);
    expect(screen.getByTitle(/dependent tasks will run on the last result/)).toBeTruthy();
  });
});
