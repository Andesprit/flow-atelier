// @vitest-environment jsdom
//
// Designer-level recovery: Undo/Redo must change what the canvas shows *and*
// what the browser draft holds, so reopening the designer continues from the
// state the user chose rather than resurrecting an edit they undid.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DRAFT_CONDUIT_STORAGE_KEY } from "@/constants/dashboard";
import type { Conduit, CreateConduitRequest } from "@/types/conduit";

const ORIGINAL = "printf 'original\\n'";
const CHANGED = "printf 'changed\\n'";

const saved: Conduit = {
  name: "greet",
  description: "one bash task",
  inputs: {},
  tasks: [
    {
      name: "build",
      tool: "tool:bash",
      description: "print a line",
      task: ORIGINAL,
      dependsOn: [],
      position: { x: 0, y: 0 },
    },
  ],
};

vi.mock("@/services/conduits", async (orig) => ({
  ...(await orig<typeof import("@/services/conduits")>()),
  clearConduitCache: vi.fn(),
  fetchConduits: vi.fn(async () => [structuredClone(saved)]),
}));

const createConduit = vi.fn(async (_payload: CreateConduitRequest) => structuredClone(saved));
const updateConduit = vi.fn(async (_payload: CreateConduitRequest) => structuredClone(saved));
vi.mock("@/services/api/conduits", async (orig) => ({
  ...(await orig<typeof import("@/services/api/conduits")>()),
  // Indirected so the hoisted factory does not capture the spies too early.
  createConduit: (payload: CreateConduitRequest) => createConduit(payload),
  updateConduit: (payload: CreateConduitRequest) => updateConduit(payload),
}));

beforeAll(() => {
  // jsdom ships none of the layout APIs ReactFlow and the responsive layout
  // need; the designer renders blank without them.
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  (globalThis as { DOMMatrixReadOnly?: unknown }).DOMMatrixReadOnly ??= class {
    m22 = 1;
  };
  HTMLElement.prototype.scrollIntoView ??= () => {};
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

import { ConduitProvider } from "@/services/ConduitProvider";
import { Designer } from "@/features/designer/Designer";

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  createConduit.mockClear();
  updateConduit.mockClear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** Mounts the designer and lets the conduit fetch settle. */
async function mountDesigner() {
  const utils = render(
    <MemoryRouter>
      <ConduitProvider>
        <Designer />
      </ConduitProvider>
    </MemoryRouter>,
  );
  await act(async () => {});
  return utils;
}

/** Loads the stored conduit through the real "open conduit" dialog. */
function openSaved() {
  fireEvent.click(screen.getByTestId("open-conduit"));
  fireEvent.click(screen.getByText("greet"));
  // Close the coalescing window, as any real user would by pausing, so the
  // following edit is its own undo step rather than part of the open.
  act(() => {
    vi.advanceTimersByTime(400);
  });
}

/** Selects the only task so the inspector shows its command. */
function selectTask() {
  fireEvent.click(screen.getByTestId("task-node"));
}

function commandField() {
  return screen.getByTestId("designer-inspector").querySelector("textarea")!;
}

function setCommand(value: string) {
  fireEvent.change(commandField(), { target: { value } });
}

function undoButton() {
  return screen.getByTitle("Undo (Ctrl+Z)");
}

function redoButton() {
  return screen.getByTitle("Redo (Ctrl+Shift+Z / Ctrl+Y)");
}

function pressUndo(target: Element | Window = window) {
  fireEvent.keyDown(target, { key: "z", ctrlKey: true });
}

function draftCommand(): string | undefined {
  const raw = localStorage.getItem(DRAFT_CONDUIT_STORAGE_KEY);
  return raw ? (JSON.parse(raw) as Conduit).tasks[0]?.task : undefined;
}

function previewYaml() {
  fireEvent.click(screen.getByTestId("preview-yaml"));
  const text = screen.getByTestId("yaml-contents").textContent ?? "";
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  return text;
}

describe("Designer recovery", () => {
  it("undoes an edit immediately and keeps the draft in step", async () => {
    await mountDesigner();
    openSaved();
    selectTask();
    setCommand(CHANGED);
    expect(draftCommand()).toBe(CHANGED);

    // No wait: the edit is still inside the 400 ms coalescing window.
    fireEvent.click(undoButton());
    expect(commandField().value).toBe(ORIGINAL);
    const yaml = previewYaml();
    expect(yaml).toContain("original");
    expect(yaml).not.toContain("changed");
    expect(draftCommand()).toBe(ORIGINAL);

    // The cancelled debounce must not write the undone edit back.
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(draftCommand()).toBe(ORIGINAL);
  });

  it("restores the undone command after leaving and reopening the designer", async () => {
    const { unmount } = await mountDesigner();
    openSaved();
    selectTask();
    setCommand(CHANGED);
    fireEvent.click(undoButton());
    unmount();

    await mountDesigner();
    selectTask();
    expect(commandField().value).toBe(ORIGINAL);
  });

  it("restores a redone command after reopening the designer", async () => {
    const { unmount } = await mountDesigner();
    openSaved();
    selectTask();
    setCommand(CHANGED);
    fireEvent.click(undoButton());
    expect(draftCommand()).toBe(ORIGINAL);
    fireEvent.click(redoButton());
    expect(commandField().value).toBe(CHANGED);
    expect(draftCommand()).toBe(CHANGED);
    unmount();

    await mountDesigner();
    selectTask();
    expect(commandField().value).toBe(CHANGED);
  });

  it("undoes a burst of edits atop older history from the keyboard", async () => {
    await mountDesigner();
    openSaved();
    selectTask();
    setCommand("printf 'one\\n'");
    act(() => {
      vi.advanceTimersByTime(400);
    });
    setCommand("printf 'two\\n'");
    setCommand("printf 'three\\n'");

    pressUndo();
    expect(commandField().value).toBe("printf 'one\\n'");
    expect(draftCommand()).toBe("printf 'one\\n'");
    pressUndo();
    expect(commandField().value).toBe(ORIGINAL);
    expect(draftCommand()).toBe(ORIGINAL);
  });

  it("leaves native text editing to the browser", async () => {
    await mountDesigner();
    openSaved();
    selectTask();
    setCommand(CHANGED);

    // Focus is in the command textarea: Ctrl+Z belongs to the field.
    pressUndo(commandField());
    expect(commandField().value).toBe(CHANGED);

    fireEvent.click(screen.getByTestId("new-conduit"));
    pressUndo(screen.getByLabelText("name"));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(draftCommand()).toBe(CHANGED);

    const editable = document.body.appendChild(document.createElement("div"));
    Object.defineProperty(editable, "isContentEditable", { value: true });
    pressUndo(editable);
    expect(draftCommand()).toBe(CHANGED);
    editable.remove();
  });

  it("does not call the API on undo or redo", async () => {
    await mountDesigner();
    openSaved();
    selectTask();
    setCommand(CHANGED);
    fireEvent.click(undoButton());
    fireEvent.click(redoButton());
    expect(createConduit).not.toHaveBeenCalled();
    expect(updateConduit).not.toHaveBeenCalled();
  });

  it("saves the undone state and clears the draft for good", async () => {
    await mountDesigner();
    openSaved();
    selectTask();
    setCommand(CHANGED);
    fireEvent.click(undoButton());

    await act(async () => {
      fireEvent.click(screen.getByTestId("designer-save"));
    });
    expect(updateConduit).toHaveBeenCalledTimes(1);
    expect(updateConduit.mock.calls[0]?.[0].tasks[0]?.task).toBe(ORIGINAL);
    expect(localStorage.getItem(DRAFT_CONDUIT_STORAGE_KEY)).toBeNull();

    // Nothing pending may recreate the draft the save just cleared.
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(localStorage.getItem(DRAFT_CONDUIT_STORAGE_KEY)).toBeNull();
  });

  it("keeps the draft when saving fails so the user can retry", async () => {
    updateConduit.mockRejectedValueOnce(new Error("backend down"));
    await mountDesigner();
    openSaved();
    selectTask();
    setCommand(CHANGED);
    fireEvent.click(undoButton());

    await act(async () => {
      fireEvent.click(screen.getByTestId("designer-save"));
    });
    expect(draftCommand()).toBe(ORIGINAL);

    await act(async () => {
      fireEvent.click(screen.getByTestId("designer-save"));
    });
    expect(updateConduit).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem(DRAFT_CONDUIT_STORAGE_KEY)).toBeNull();
  });
});
