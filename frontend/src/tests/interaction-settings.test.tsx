import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { InteractionSettings } from "@/features/designer/components/InteractionSettings";
import { ToolPanel } from "@/features/designer/components/ToolPanel";
import { interactionError } from "@/features/designer/interaction-policy";
import type { InteractionPolicy } from "@/types/conduit";

// jsdom does not implement scrolling; Radix scrolls the focused menu option.
HTMLElement.prototype.scrollIntoView ??= () => {};

const configured: InteractionPolicy = {
  questions: "hybrid", permissions: "human",
  supervisor: { tool: "harness:custom-agent", instructions: "Keep public API stable", maxReplies: 3 },
};

function Editor({ initial = configured }: { initial?: InteractionPolicy }) {
  const [policy, setPolicy] = useState<InteractionPolicy | null>(initial);
  return <>
    <InteractionSettings value={policy} onChange={setPolicy} />
    <button disabled={!!interactionError(policy)}>Save</button>
    <pre data-testid="policy">{JSON.stringify(policy)}</pre>
  </>;
}

function openSettings() {
  fireEvent.click(screen.getByText("interaction"));
}

function selectMode(label: string, option: string) {
  fireEvent.keyDown(screen.getByRole("combobox", { name: label }), { key: "Enter" });
  fireEvent.click(screen.getByRole("option", { name: option }));
}

afterEach(cleanup);

describe("Interaction settings", () => {
  it("keeps settings collapsed until requested", () => {
    render(<Editor />);
    expect(document.querySelector("details")?.open).toBe(false);
    expect(screen.getByText("Questions: Supervisor can ask me")).toBeTruthy();
    expect(screen.getByText("Tool approvals: Human always")).toBeTruthy();
    openSettings();
    expect(document.querySelector("details")?.open).toBe(true);
    expect(screen.getByRole("combobox", { name: "Questions" })).toBeTruthy();
  });

  it("preserves instructions and limits when the policy is disabled and enabled", () => {
    render(<Editor />);
    openSettings();
    fireEvent.click(screen.getByRole("checkbox", { name: "Use conduit policy" }));
    expect(screen.getByTestId("policy").textContent).toBe("null");
    fireEvent.click(screen.getByRole("checkbox", { name: "Use conduit policy" }));
    expect(JSON.parse(screen.getByTestId("policy").textContent!)).toEqual(configured);
  });

  it("reports invalid names beside the field and prevents saving", () => {
    render(<Editor />);
    openSettings();
    const input = screen.getByRole("textbox", { name: "Supervisor harness" });
    fireEvent.change(input, { target: { value: "codex" } });
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("status").textContent).toContain("harness:codex");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: "harness:my-private-agent" } });
    expect(screen.queryByRole("status")).toBeNull();
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("does not submit an unused supervisor but restores it when switching back", () => {
    render(<Editor />);
    openSettings();
    selectMode("Questions", "Human always");
    expect(JSON.parse(screen.getByTestId("policy").textContent!).supervisor).toBeUndefined();
    selectMode("Questions", "Supervisor always");
    expect(JSON.parse(screen.getByTestId("policy").textContent!).supervisor).toEqual(configured.supervisor);
    expect(screen.getByRole("textbox", { name: "Instructions" })).toBeTruthy();
  });

  it("does not restore another conduit's policy after switching conduits", () => {
    const onInteractionChange = vi.fn();
    const props = { conduitInputs: {}, onAddTask: vi.fn(), onAddInput: vi.fn(), onRemoveInput: vi.fn(), onInteractionChange };
    const first = { name: "first", description: "", inputs: {}, tasks: [], interaction: configured };
    const { rerender } = render(<ToolPanel {...props} conduit={first} />);
    openSettings();
    fireEvent.click(screen.getByRole("checkbox", { name: "Use conduit policy" }));
    rerender(<ToolPanel {...props} conduit={{ ...first, name: "second", interaction: null }} />);
    expect(screen.getByText("Task defaults")).toBeTruthy();
    expect(document.querySelector("details")?.open).toBe(false);
    openSettings();
    fireEvent.click(screen.getByRole("checkbox", { name: "Use conduit policy" }));
    expect(onInteractionChange).toHaveBeenLastCalledWith({ questions: "human", permissions: "approve_all" });
  });
});
