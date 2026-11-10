export interface PriorFlow {
  flowId: string;
  conduitName: string;
  startedAt: number;
  finishedAt?: number;
  duration?: number;
  status: "running" | "done" | "failed" | "cancelled";
  author?: string;
}
