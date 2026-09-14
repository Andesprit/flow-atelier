import { describe, it, expect, beforeAll, afterAll, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { FlowDrawer, type FlowDrawerTask } from "@/components/FlowDrawer";
import type { LogEntry } from "@/types/task";

// Radix's ScrollArea observes its viewport; jsdom ships no ResizeObserver.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

const LINE_PX = 100;
const VIEWPORT_PX = 300;

// jsdom does no layout: every element is 0px tall, so a scroll is a no-op and
// nothing about following could be observed. Give every box a geometry of one
// line per child and a fixed viewport; scrollTop itself is stored by jsdom.
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.childElementCount * LINE_PX;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return VIEWPORT_PX;
    },
  });
});
afterAll(() => {
  delete (HTMLElement.prototype as { scrollHeight?: unknown }).scrollHeight;
  delete (HTMLElement.prototype as { clientHeight?: unknown }).clientHeight;
});
afterEach(cleanup);

const T = Date.now();
const tasks: FlowDrawerTask[] = [{ name: "agent", status: "running" }];

/** A running flow whose `agent` task has printed `n` lines. */
function logs(n: number, markers: string[] = []): LogEntry[] {
  return [
    { t: T, text: "▸ flow started", level: "info" },
    ...markers.map((text) => ({ t: T, text, level: "info" as const })),
    ...Array.from({ length: n }, (_, i) => ({
      t: T + i,
      text: `line ${i}`,
      level: "info" as const,
      task: "agent",
    })),
  ];
}

function drawer(lines: LogEntry[]) {
  return <FlowDrawer open onClose={() => {}} title="goal_loop" tasks={tasks} logLines={lines} />;
}

/** The reader drags the box: jsdom fires no scroll event on its own. */
function scrollTo(box: HTMLElement, top: number) {
  box.scrollTop = top;
  fireEvent.scroll(box);
}

const bottomOf = (box: HTMLElement) => box.scrollHeight - box.clientHeight;

/**
 * The box for the task that is running streams the agent's output; it is the
 * one a developer watches. It has to keep up with the tail on its own, and it
 * has to stop doing so while the developer reads something further up.
 */
describe("FlowDrawer log boxes follow their tail", () => {
  it("scrolls the running task's box to the newest line as lines arrive", () => {
    const { rerender } = render(drawer(logs(10)));
    const box = screen.getByTestId("task-logs");
    expect(box.scrollTop).toBe(10 * LINE_PX);

    rerender(drawer(logs(11)));
    expect(box.scrollTop).toBe(11 * LINE_PX);
  });

  it("stays put while the reader has scrolled up", () => {
    const { rerender } = render(drawer(logs(10)));
    const box = screen.getByTestId("task-logs");
    scrollTo(box, 0);

    rerender(drawer(logs(11)));
    expect(box.scrollTop).toBe(0);
  });

  it("follows again once the reader returns to the bottom", () => {
    const { rerender } = render(drawer(logs(10)));
    const box = screen.getByTestId("task-logs");
    scrollTo(box, 0);
    rerender(drawer(logs(11)));
    scrollTo(box, bottomOf(box));

    rerender(drawer(logs(12)));
    expect(box.scrollTop).toBe(12 * LINE_PX);
  });

  it("keeps the flat logs box following too", () => {
    const { rerender } = render(drawer(logs(10)));
    const box = screen.getByTestId("drawer-logs");
    // Only untagged lines and task markers land here: one line so far.
    expect(box.scrollTop).toBe(1 * LINE_PX);

    rerender(drawer(logs(10, ["▸ setup", "✓ setup"])));
    expect(box.scrollTop).toBe(3 * LINE_PX);
  });
});
