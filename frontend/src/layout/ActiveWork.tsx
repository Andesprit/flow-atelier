import { useState } from "react";
import { Link } from "react-router-dom";
import { cn } from "@lib/cn";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useAllLiveRuns, type OpenRunState, type RunOrigin } from "@/context/LiveRunsContext";
import { waitingFlowIds } from "@/hooks/useConduit";

interface Item {
  origin: RunOrigin;
  flowId: string;
  conduitName: string;
  waiting: boolean;
}

/** Top-level runs still going, the ones parked on a person first. */
function useActiveItems(): Item[] {
  const all = useAllLiveRuns();
  const items: Item[] = [];
  for (const origin of ["dashboard", "kanban"] as const) {
    // Waiting is worked out per session: a child run's parent is always in the
    // same session as the child.
    const runs = all.filter((x) => x.origin === origin).map((x) => x.run);
    const waiting = waitingFlowIds(runs);
    for (const r of runs) {
      if (r.parentFlowId || r.status !== "running") continue;
      items.push({ origin, flowId: r.flowId, conduitName: r.conduitName, waiting: waiting.has(r.flowId) });
    }
  }
  return items.sort((a, b) => Number(b.waiting) - Number(a.waiting));
}

const describe = (i: Item) =>
  `${i.conduitName || i.flowId} ${i.waiting ? "is waiting on you" : "is running"} (${i.origin})`;

// Below sm it takes the wordmark's place (see TopBar) with a short label: the
// three nav targets already fill a 390px bar.
const chip =
  "inline-flex h-11 min-w-11 shrink-0 items-center justify-center gap-1.5 rounded-sm border px-2 font-mono text-mini uppercase tracking-[0.08em] focus-visible:outline-2 focus-visible:outline-primary max-sm:mr-auto sm:px-2.5";

/**
 * Shown on every screen while work started in this tab is still going: the
 * way back to the drawer or card that has its controls, and the answer box when
 * it is waiting on someone. Following it never starts, resumes or re-runs work.
 */
export function ActiveWork() {
  const items = useActiveItems();
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;

  const waiting = items.filter((i) => i.waiting).length;
  const text = (
    <>
      <span aria-hidden className="sm:hidden">{waiting ? `?${waiting}` : `●${items.length}`}</span>
      <span aria-hidden className="hidden sm:inline">
        {waiting ? `? ${waiting} waiting` : `● ${items.length} running`}
      </span>
    </>
  );
  const tone = waiting ? "border-primary text-primary" : "border-border text-muted-foreground hover:text-foreground";

  if (items.length === 1) {
    const [only] = items;
    return (
      <Link
        to={`/${only.origin}`}
        state={{ openFlowId: only.flowId } satisfies OpenRunState}
        data-testid="active-work"
        aria-label={`${describe(only)}. Open its controls.`}
        title={describe(only)}
        className={cn(chip, tone)}
      >
        {text}
      </Link>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        data-testid="active-work"
        aria-label={`${items.length} runs going, ${waiting} waiting on you. Choose one to open its controls.`}
        className={cn(chip, tone, "cursor-pointer")}
      >
        {text}
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-1">
        <ul aria-label="Runs going">
          {items.map((i) => (
            <li key={`${i.origin}:${i.flowId}`}>
              <Link
                to={`/${i.origin}`}
                state={{ openFlowId: i.flowId } satisfies OpenRunState}
                onClick={() => setOpen(false)}
                aria-label={`${describe(i)}. Open its controls.`}
                className="flex min-h-11 flex-col justify-center rounded-sm px-2 py-1.5 hover:bg-muted focus-visible:outline-2 focus-visible:outline-primary"
              >
                <span className="truncate font-mono text-data text-foreground">
                  {i.waiting ? "? " : "● "}
                  {i.conduitName || i.flowId}
                </span>
                <span className="font-mono text-mini uppercase tracking-[0.08em] text-muted-foreground">
                  {i.waiting ? "waiting on you" : "running"} · {i.origin}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
