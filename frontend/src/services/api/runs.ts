import { fetchJson, USE_MOCK, BASE_URL } from "@/services/client";
import type { RunView, TaskLog } from "@/types/run";

// The mock backend has no saved runs to read, so the run page says so instead
// of rendering invented data.
const NO_MOCK = "The run page reads saved runs from `atelier serve`; mock data has none.";

export async function getRunView(flowId: string): Promise<RunView> {
  if (USE_MOCK) throw new Error(NO_MOCK);
  return fetchJson<RunView>(`${BASE_URL}/flows/${encodeURIComponent(flowId)}`, undefined, {
    method: "GET",
  });
}

export async function getTaskLog(flowId: string, task: string): Promise<TaskLog> {
  if (USE_MOCK) throw new Error(NO_MOCK);
  return fetchJson<TaskLog>(
    `${BASE_URL}/flows/${encodeURIComponent(flowId)}/tasks/${encodeURIComponent(task)}/log`,
    undefined,
    { method: "GET" },
  );
}
