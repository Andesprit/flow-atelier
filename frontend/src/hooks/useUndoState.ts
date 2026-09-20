import { useState, useCallback, useEffect, useRef } from "react";

const MAX_HISTORY = 50;
const DEBOUNCE_MS = 400;

/**
 * Wraps useState with Ctrl+Z–friendly undo.
 * Rapid setter calls (e.g. text typing) are debounced into one history entry.
 */
export function useUndoState<T>(initial: T | (() => T)) {
  const [state, setStateRaw] = useState(initial);

  const history = useRef<T[]>([]);
  const future = useRef<T[]>([]);
  const pending = useRef<T | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  const flush = useCallback(() => {
    if (pending.current !== null) {
      history.current.push(pending.current);
      if (history.current.length > MAX_HISTORY) history.current.shift();
      pending.current = null;
    }
  }, []);

  const setState = useCallback(
    (value: T | ((prev: T) => T)) => {
      setStateRaw((prev) => {
        const next =
          typeof value === "function"
            ? (value as (p: T) => T)(prev)
            : value;

        future.current = [];

        // First mutation in a burst captures the pre-mutation state
        if (pending.current === null) pending.current = structuredClone(prev);

        clearTimeout(timer.current);
        timer.current = setTimeout(flush, DEBOUNCE_MS);
        return next;
      });
    },
    [flush],
  );

  const undo = useCallback(() => {
    clearTimeout(timer.current);
    // The still-open burst is the most recent entry, so it has to be consumed
    // before older history. Dropping it (the previous behaviour) made the last
    // edit permanently un-undoable, or skipped straight past it to an older
    // state, depending on whether history already held anything.
    let target: T;
    if (pending.current !== null) {
      target = pending.current;
      pending.current = null;
    } else if (history.current.length > 0) {
      target = history.current.pop()!;
    } else {
      return;
    }
    setStateRaw((prev) => {
      future.current.push(prev);
      return target;
    });
  }, []);

  const redo = useCallback(() => {
    if (future.current.length === 0) return;
    clearTimeout(timer.current);
    pending.current = null;
    setStateRaw((prev) => {
      const next = future.current.pop()!;
      history.current.push(prev);
      if (history.current.length > MAX_HISTORY) history.current.shift();
      return next;
    });
  }, []);

  // A burst left open at unmount must not fire its flush afterwards.
  useEffect(() => () => clearTimeout(timer.current), []);

  return [state, setState, undo, redo] as const;
}
