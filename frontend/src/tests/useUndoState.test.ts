// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useUndoState } from "@/hooks/useUndoState";
import { vi } from "vitest";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useUndoState", () => {
  it("returns initial value", () => {
    const { result } = renderHook(() => useUndoState("hello"));
    expect(result.current[0]).toBe("hello");
  });

  it("supports lazy initializer", () => {
    const { result } = renderHook(() => useUndoState(() => "lazy"));
    expect(result.current[0]).toBe("lazy");
  });

  it("sets state with direct value", () => {
    const { result } = renderHook(() => useUndoState("hello"));
    act(() => {
      result.current[1]("world");
    });
    expect(result.current[0]).toBe("world");
  });

  it("sets state with functional updater", () => {
    const { result } = renderHook(() => useUndoState("hello"));
    act(() => {
      result.current[1]((prev) => prev + "!");
    });
    expect(result.current[0]).toBe("hello!");
  });

  it("undoes after debounce flushes", () => {
    const { result } = renderHook(() => useUndoState("hello"));
    act(() => {
      result.current[1]("world");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current[2](); // undo
    });
    expect(result.current[0]).toBe("hello");
  });

  it("undoes the first edit before the debounce flushes", () => {
    const { result } = renderHook(() => useUndoState("hello"));
    act(() => {
      result.current[1]("world");
    });
    // The open burst is the newest entry: undo must consume it rather than
    // discard it, even though nothing has reached history yet.
    act(() => {
      result.current[2](); // undo
    });
    expect(result.current[0]).toBe("hello");
    // The cancelled timer must not push the consumed group back into history.
    act(() => {
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current[2]();
    });
    expect(result.current[0]).toBe("hello");
    // Redo still restores the edit that was undone.
    act(() => {
      result.current[3]();
    });
    expect(result.current[0]).toBe("world");
  });

  it("undoes a pending burst before older history", () => {
    const { result } = renderHook(() => useUndoState("a"));
    act(() => {
      result.current[1]("b");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current[1]("c"); // still pending
    });
    act(() => {
      result.current[2](); // undo consumes the pending group
    });
    expect(result.current[0]).toBe("b");
    act(() => {
      result.current[2](); // then the flushed entry
    });
    expect(result.current[0]).toBe("a");
    act(() => {
      result.current[3]();
    });
    expect(result.current[0]).toBe("b");
    act(() => {
      result.current[3]();
    });
    expect(result.current[0]).toBe("c");
  });

  it("undoes a whole pending burst as one entry", () => {
    const { result } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[1]("a");
      result.current[1]("b");
      result.current[1]("c");
    });
    act(() => {
      result.current[2](); // undo before the 400ms window closes
    });
    expect(result.current[0]).toBe("initial");
    act(() => {
      result.current[3](); // redo restores the last state of the burst
    });
    expect(result.current[0]).toBe("c");
  });

  it("a new edit after an early undo clears redo", () => {
    const { result } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[1]("a");
    });
    act(() => {
      result.current[2](); // undo before flush
    });
    expect(result.current[0]).toBe("initial");
    act(() => {
      result.current[1]("b");
    });
    act(() => {
      result.current[3](); // redo — future was cleared by the new edit
    });
    expect(result.current[0]).toBe("b");
  });

  it("cancels the pending timer on undo, redo and unmount", () => {
    const { result, unmount } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[1]("a");
    });
    expect(vi.getTimerCount()).toBe(1);
    act(() => {
      result.current[2](); // undo
    });
    expect(vi.getTimerCount()).toBe(0);
    act(() => {
      result.current[3](); // redo
    });
    expect(vi.getTimerCount()).toBe(0);
    act(() => {
      result.current[1]("b");
    });
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("is a no-op when undo is called with empty history", () => {
    const { result } = renderHook(() => useUndoState("hello"));
    act(() => {
      result.current[2](); // undo with nothing to undo
    });
    expect(result.current[0]).toBe("hello");
  });

  it("coalesces rapid sets into one history entry", () => {
    const { result } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[1]("a");
      result.current[1]("b");
      result.current[1]("c");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    // Only one undo should bring us back to "initial"
    act(() => {
      result.current[2](); // undo
    });
    expect(result.current[0]).toBe("initial");
  });

  it("redo reverses undo", () => {
    const { result } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[1]("a");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(result.current[0]).toBe("a");
    act(() => {
      result.current[2](); // undo
    });
    expect(result.current[0]).toBe("initial");
    act(() => {
      result.current[3](); // redo
    });
    expect(result.current[0]).toBe("a");
  });

  it("redo is a no-op on empty future", () => {
    const { result } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[3](); // redo with nothing undone
    });
    expect(result.current[0]).toBe("initial");
  });

  it("new edit clears redo", () => {
    const { result } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[1]("a");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current[2](); // undo
    });
    expect(result.current[0]).toBe("initial");
    act(() => {
      result.current[1]("b"); // new edit after undo
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(result.current[0]).toBe("b");
    act(() => {
      result.current[3](); // redo — future was cleared
    });
    expect(result.current[0]).toBe("b");
  });

  it("multi-step undo/redo symmetry", () => {
    const { result } = renderHook(() => useUndoState("initial"));
    act(() => {
      result.current[1]("a");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current[1]("b");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current[1]("c");
    });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(result.current[0]).toBe("c");
    // Undo 3 times back to initial
    act(() => { result.current[2](); });
    expect(result.current[0]).toBe("b");
    act(() => { result.current[2](); });
    expect(result.current[0]).toBe("a");
    act(() => { result.current[2](); });
    expect(result.current[0]).toBe("initial");
    // Redo 3 times forward to c
    act(() => { result.current[3](); });
    expect(result.current[0]).toBe("a");
    act(() => { result.current[3](); });
    expect(result.current[0]).toBe("b");
    act(() => { result.current[3](); });
    expect(result.current[0]).toBe("c");
  });

  it("enforces MAX_HISTORY=50 cap", () => {
    const { result } = renderHook(() => useUndoState(0));
    // Make 51 distinct state changes with debounce flushes
    for (let i = 1; i <= 51; i++) {
      act(() => {
        result.current[1](i);
      });
      act(() => {
        vi.advanceTimersByTime(400);
      });
    }
    // State should be 51
    expect(result.current[0]).toBe(51);
    // 50 undos should work (history capped at 50, first entry was dropped)
    for (let i = 0; i < 50; i++) {
      act(() => {
        result.current[2]();
      });
    }
    // After 50 undos we're at state 1 (the first entry was evicted)
    expect(result.current[0]).toBe(1);
    // 51st undo is a no-op — history exhausted
    act(() => {
      result.current[2]();
    });
    expect(result.current[0]).toBe(1);
  });
});
