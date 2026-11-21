export interface PriorFlow {
  flowId: string;
  project?: string;
  conduitName: string;
  startedAt: number;
  finishedAt?: number;
  duration?: number;
  status: "running" | "done" | "failed" | "cancelled" | "crashed" | "waiting";
  author?: string;
}
