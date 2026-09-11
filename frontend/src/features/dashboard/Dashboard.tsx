import { useState, useEffect } from "react";
import { toast } from "sonner";
import { ConduitPicker } from "@/features/dashboard/components/conduit-picker";
import { InputForm } from "@/features/dashboard/components/input-form";
import { FlowHistory } from "@/features/dashboard/components/flow-history";
import { Button } from "@/components/ui/button";
import { useConduits, getConduitSync } from "@/services/ConduitProvider";
import { BASE_URL } from "@/config/env";
import { useConduit } from "@/hooks/useConduit";
import { useRunTitle } from "@/hooks/useRunTitle";
import { fetchSchedules } from "@/services/conduits";
import { createSchedule, deleteSchedule } from "@/services/api/schedules";
import { ScheduleDialog } from "@/features/dashboard/components/schedule-dialog";
import type { ScheduleConfig, ScheduledJob } from "@/types/schedule";

export default function Dashboard() {
  const { conduits, loading, error, refresh } = useConduits();
  const [selected, setSelected] = useState(conduits[0]?.name ?? "");
  const [scheduledJobs, setScheduledJobs] = useState<ScheduledJob[]>([]);
  const [addScheduleOpen, setAddScheduleOpen] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [autoOpenFlowId, setAutoOpenFlowId] = useState<string | undefined>();
  const conduit = getConduitSync(selected, conduits) ?? conduits[0];

  const { run, cancel, resume, answerHITL, answerAgentInput, liveRuns } = useConduit({
    onFlowComplete: () => {
      setRefreshKey((k) => k + 1);
    },
    // Runs the user started here — scheduled runs arrive as their own message
    // type, so a background schedule firing never pops the drawer open.
    onFlowStarted: (flowId) => setAutoOpenFlowId(flowId),
    onError: (message) => toast.error(message),
  });
  useRunTitle(liveRuns);

  // When the API is unreachable both fetches fail for the same reason, and the
  // conduits failure already renders a full cause-level message inline; a
  // second toast repeating it is noise. The two requests race, so we can't
  // decide at rejection time — the conduits result may not be in yet. Record
  // the failure and let an effect fire the toast only once the conduits load
  // has resolved, and only if it resolved successfully.
  const [schedulesFailed, setSchedulesFailed] = useState(false);
  useEffect(() => {
    if (schedulesFailed && !loading && !error) {
      toast.error("Couldn't load schedules", { id: "schedules-load" });
      setSchedulesFailed(false);
    }
  }, [schedulesFailed, loading, error]);

  const refreshAll = () => {
    setRefreshKey((k) => k + 1);
    fetchSchedules()
      .then(setScheduledJobs)
      .catch(() => setSchedulesFailed(true));
  };

  useEffect(() => {
    let ignore = false;
    fetchSchedules()
      .then((jobs) => {
        if (!ignore) setScheduledJobs(jobs);
      })
      .catch(() => {
        if (!ignore) setSchedulesFailed(true);
      });
    return () => {
      ignore = true;
    };
  }, []);

  useEffect(() => {
    if (conduits.length > 0 && !conduits.find((c) => c.name === selected)) {
      setSelected(conduits[0].name);
    }
  }, [conduits, selected]);

  const handleSchedule = (inputs: Record<string, string>, config: ScheduleConfig, scheduleConduitName?: string, runPath?: string) => {
    const conduitName = scheduleConduitName ?? conduit.name;

    createSchedule({
      conduitName,
      inputs,
      runPath,
      schedule: config,
    })
      .then((job) => {
        setScheduledJobs((prev) => [job, ...prev]);
        refreshAll();
        toast.success(`Scheduled ${job.schedule.name || conduitName}`);
      })
      .catch((e) => {
        toast.error(e instanceof Error ? e.message : "Failed to create schedule");
      });
  };

  const handleDeleteJob = (id: string) => {
    const job = scheduledJobs.find((j) => j.id === id);
    deleteSchedule(id).then(() => {
      setScheduledJobs((prev) => prev.filter((j) => j.id !== id));
      refreshAll();
      toast.success(`Deleted schedule ${job?.schedule.name || job?.conduitName || ""}`);
    }).catch(() => {
      toast.error("Failed to delete schedule");
    });
  };

  const startRun = (conduitName: string, inputs: Record<string, string>, runPath: string) => {
    run(conduitName, inputs, runPath);
    toast.success(`Started ${conduitName}`);
  };

  const handleRun = (inputs: Record<string, string>, runPath: string) =>
    startRun(conduit.name, inputs, runPath);

  const handleResume = (flowId: string, conduitName?: string) => {
    resume(flowId, conduitName);
    toast.success("Resuming flow");
  };

  return (
    <div data-route="dashboard" className="min-h-[calc(100dvh-3.5rem)]">
      <section
        data-testid="dashboard-main"
        className="px-4 py-6 lg:px-10 lg:py-10"
      >
        {/* The 52px serif "Run a conduit" that used to sit here cost ~22% of
            the fold to repeat what the active nav item already says. The
            wordmark in the top bar carries the brand voice now. */}
        <header className="mx-auto mb-6 flex max-w-[1280px] items-baseline justify-between gap-6 border-b border-border pb-4 lg:mb-8">
          <h1 className="font-mono text-label uppercase tracking-[0.14em] text-foreground">
            run a conduit
          </h1>
          <div className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
            {/* An em dash, not 0: during a failure the count is unknown, and
                "0 conduits" asserted something untrue. */}
            {loading ? "loading…" : error ? "— conduits" : `${conduits.length} conduits`}
          </div>
        </header>

        {error ? (
          <div data-testid="dashboard-error" className="mx-auto max-w-[560px] py-20 text-center">
            <div className="mb-3 font-mono text-label uppercase tracking-[0.12em] text-destructive">
              Can't reach the flow-atelier API
            </div>
            <div className="mb-6 text-body leading-relaxed text-muted-foreground">
              Tried <span className="font-mono text-data text-foreground">{BASE_URL}</span>. Check
              that the server is running, then retry.
            </div>
            <details className="mb-6 text-left">
              <summary className="cursor-pointer font-mono text-mini uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground">
                details
              </summary>
              <p className="mt-2 font-mono text-mini leading-relaxed text-muted-foreground">
                {error}
              </p>
            </details>
            <Button variant="outline" onClick={refresh}>
              retry
            </Button>
          </div>
        ) : loading ? (
          <div data-testid="dashboard-loading" className="mx-auto grid max-w-[1280px] grid-cols-1 gap-10 lg:gap-16 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
            <section>
              <div className="space-y-3">
                <div className="h-4 w-28 animate-pulse rounded bg-muted" />
                {[1, 2, 3].map((i) => (
                  <div key={i} className="h-12 animate-pulse rounded-sm border border-border bg-muted/40" />
                ))}
              </div>
              <div className="mt-10 space-y-4">
                <div className="h-4 w-20 animate-pulse rounded bg-muted" />
                {[1, 2].map((i) => (
                  <div key={i} className="h-10 animate-pulse rounded-sm border border-border bg-muted/40" />
                ))}
              </div>
            </section>
            <section>
              <div className="space-y-3">
                <div className="h-4 w-32 animate-pulse rounded bg-muted" />
                {[1, 2, 3].map((i) => (
                  <div key={i} className="h-8 animate-pulse rounded-sm border border-border bg-muted/40" />
                ))}
              </div>
            </section>
          </div>
        ) : (
        <div className="mx-auto grid max-w-[1280px] grid-cols-1 gap-10 lg:gap-16 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <section>
            <ConduitPicker selected={selected} onSelect={setSelected} />
            {conduit && (
              <InputForm conduit={conduit} onRun={handleRun} onSchedule={handleSchedule} />
            )}
          </section>
          <section>
            <FlowHistory
              scheduledJobs={scheduledJobs}
              onDeleteJob={handleDeleteJob}
              onAddSchedule={() => setAddScheduleOpen(true)}
              refreshKey={refreshKey}
              autoOpenFlowId={autoOpenFlowId}
              liveRuns={liveRuns}
              onRespondToHitl={answerHITL}
              onAnswerAgentInput={answerAgentInput}
              onCancelRun={cancel}
              onResumeRun={handleResume}
              onRunAgain={startRun}
            />
          </section>
        </div>
        )}
      </section>

      <ScheduleDialog
        conduitName=""
        open={addScheduleOpen}
        onOpenChange={setAddScheduleOpen}
        onConfirm={(config, name, inputs, runPath) => handleSchedule(inputs, config, name, runPath)}
      />
    </div>
  );
}
