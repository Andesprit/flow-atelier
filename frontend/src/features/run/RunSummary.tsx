import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/CopyButton";
import type { RunResult, RunSummary as Summary } from "@/types/run";

interface Props {
  summary?: Summary;
  error?: string;
  loading: boolean;
  onRetry: () => void;
  onSelectTask: (task: string) => void;
}

const BOX = "rounded-md border border-border bg-card p-4";
const PRE = "max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/40 p-3 font-mono text-data";

/** A command shown as text with a copy button; the page never runs it. */
function Command({ text, testId }: { text: string; testId: string }) {
  return (
    <div className="flex flex-wrap items-start gap-2">
      <code className="min-w-0 flex-1 break-all rounded bg-muted/40 px-2 py-1 font-mono text-data select-all">
        {text}
      </code>
      <CopyButton label="copy" getText={() => text} testId={testId} />
    </div>
  );
}

function ResultBody({ result }: { result: RunResult }) {
  if (result.state === "text") return <pre className={PRE}>{result.text}</pre>;
  const said = {
    empty: "Saved an empty result.",
    none: "No result recorded for this task.",
    missing: "Nothing saved for this task.",
  }[result.state];
  return <p className="font-mono text-data text-muted-foreground">{said}</p>;
}

/**
 * The run's saved results and what to do next, above its map and log.
 * Everything here comes from `GET /flows/<id>/summary`, which reuses
 * `atelier diagnose`; the page shows and copies commands, it never runs them.
 */
export function RunSummary({ summary, error, loading, onRetry, onSelectTask }: Props) {
  if (error) {
    return (
      <section className={BOX} data-testid="run-summary-error" aria-live="polite">
        <p className="font-mono text-data text-destructive">
          Couldn't read this run's results and next step: {error}
        </p>
        <p className="mt-1 font-mono text-label text-muted-foreground">
          The map and logs below still work.
        </p>
        <Button type="button" variant="outline" size="sm" className="mt-2" onClick={onRetry}>
          Try again
        </Button>
      </section>
    );
  }
  if (!summary) {
    return loading ? (
      <p className="font-mono text-label text-muted-foreground" data-testid="run-summary-loading">
        Reading results…
      </p>
    ) : null;
  }

  const truncated = summary.results.filter((r) => r.truncated);
  const heading = summary.final
    ? "Result"
    : summary.status === "running"
      ? "Saved so far (still running)"
      : "Saved so far";
  const commands = summary.nextSteps;

  return (
    <section className="flex flex-col gap-4" data-testid="run-summary" aria-label="Results and next step">
      {summary.settledNote && (
        <p className="font-mono text-label text-warning" data-testid="run-summary-unsettled">
          {summary.settledNote}
        </p>
      )}

      {summary.failures.length > 0 && (
        <div className={`${BOX} border-destructive`} data-testid="run-failures">
          <h2 className="font-display text-panel">What failed</h2>
          {summary.failures.map((f) => (
            <div key={f.task} className="mt-3">
              <p className="font-mono text-data">
                <button
                  type="button"
                  className="text-primary underline-offset-2 hover:underline"
                  onClick={() => onSelectTask(f.task)}
                >
                  {f.task}
                </button>
                {f.exitCode != null && ` · exit ${f.exitCode}`}
                {f.channel && ` · ${f.channel}`}
              </p>
              {f.excerpt ? (
                <pre className={`${PRE} mt-1`}>{f.excerpt}</pre>
              ) : (
                f.reason && <p className="mt-1 font-mono text-data">{f.reason}</p>
              )}
              {f.truncated && (
                <p className="font-mono text-label text-muted-foreground">
                  Trimmed; the task's log below has all of it.
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      <div className={BOX} data-testid="run-results">
        <h2 className="font-display text-panel">{heading}</h2>
        {summary.resultsUnavailable ? (
          <p className="mt-2 font-mono text-data text-destructive">{summary.resultsUnavailable}</p>
        ) : summary.results.length === 0 ? (
          <p className="mt-2 font-mono text-data text-muted-foreground">
            No task has saved a result yet.
          </p>
        ) : (
          summary.results.map((r) => (
            <div key={r.task} className="mt-3" data-testid={`run-result-${r.task}`}>
              <p className="mb-1 font-mono text-label text-muted-foreground">
                <button
                  type="button"
                  className="text-primary underline-offset-2 hover:underline"
                  onClick={() => onSelectTask(r.task)}
                >
                  {r.task}
                </button>
                {` · ${r.status}`}
                {r.truncated && ` · first ${r.text?.length ?? 0} of ${r.chars} characters`}
              </p>
              <ResultBody result={r} />
            </div>
          ))
        )}
        {truncated.length > 0 && (
          <div className="mt-3">
            <p className="mb-1 font-mono text-label text-muted-foreground">
              Shortened above. Print every result in full:
            </p>
            <Command text={summary.fullResultsCommand} testId="copy-full-results" />
          </div>
        )}
      </div>

      {commands.length > 0 && (
        <div className={BOX} data-testid="run-next-steps">
          <h2 className="font-display text-panel">Next step</h2>
          {summary.observedState === "running" && (
            <p className="mt-1 font-mono text-label text-muted-foreground">{summary.observedNote}</p>
          )}
          <p className="mt-1 font-mono text-label text-muted-foreground">
            {summary.workspace
              ? `Copied commands first change to the project folder ${summary.workspace}.`
              : summary.storeNote}
          </p>
          <ol className="mt-2 flex flex-col gap-3">
            {commands.map((step, i) => (
              <li key={i} className="flex flex-col gap-1">
                <p className="font-mono text-data">{step.what}</p>
                {step.copyText && <Command text={step.copyText} testId={`copy-step-${i}`} />}
                {step.sideEffects && (
                  <p className="font-mono text-label text-muted-foreground">
                    Effect: {step.sideEffects}
                  </p>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      {summary.unavailable.length > 0 && (
        <details className="font-mono text-label text-muted-foreground">
          <summary className="cursor-pointer">What this run did not save ({summary.unavailable.length})</summary>
          <ul className="mt-1 list-disc pl-5">
            {summary.unavailable.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
