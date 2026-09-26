import { BASE_URL, USE_MOCK, getApiToken, setApiToken } from "@/config/env";
import { toCamelCase } from "@/services/transforms";
import type { RunView, TaskLog, TaskLogUpdate } from "@/types/run";

const WS_URL = BASE_URL.replace(/^http/, "ws");
// Waits between reconnect attempts after the server goes away, in ms.
const RETRY_MS = [1000, 2000, 5000, 10000];

type FeedMessage =
  | { type: "flow"; flow: RunView }
  | { type: "task_log"; log: TaskLog }
  | { type: "task_update"; update: TaskLogUpdate }
  | { type: "error"; message: string };

export interface RunFeedHandlers {
  onFlow: (view: RunView) => void;
  onTaskLog: (log: TaskLog) => void;
  onTaskUpdate: (update: TaskLogUpdate) => void;
  onError: (message: string) => void;
}

export interface RunFeed {
  /** Follow this task's log; replaces the task followed before. */
  watch: (task: string) => void;
  close: () => void;
}

/**
 * Open the run page's live feed: `/ws/flows/<flow_id>` pushes the map and the
 * watched task's log as the run writes them. It reconnects if the server goes
 * away, and asks for the API token once if the server refuses the socket.
 */
export function openRunFeed(flowId: string, handlers: RunFeedHandlers): RunFeed {
  if (USE_MOCK) {
    queueMicrotask(() =>
      handlers.onError("The run page reads saved runs from `atelier serve`; mock data has none."),
    );
    return { watch: () => {}, close: () => {} };
  }

  let socket: WebSocket | undefined;
  let watched: string | undefined;
  let closed = false;
  let attempt = 0;
  let askedForToken = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const sendWatch = () => {
    if (watched && socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "watch", task: watched }));
    }
  };

  const connect = () => {
    const token = getApiToken();
    const query = token ? `?token=${encodeURIComponent(token)}` : "";
    socket = new WebSocket(`${WS_URL}/ws/flows/${encodeURIComponent(flowId)}${query}`);
    socket.onopen = () => {
      attempt = 0;
      sendWatch();
    };
    socket.onmessage = (ev) => {
      let msg: FeedMessage;
      try {
        msg = toCamelCase<FeedMessage>(JSON.parse(ev.data));
      } catch {
        return;
      }
      if (msg.type === "flow") handlers.onFlow(msg.flow);
      else if (msg.type === "task_log") handlers.onTaskLog(msg.log);
      else if (msg.type === "task_update") handlers.onTaskUpdate(msg.update);
      else if (msg.type === "error") handlers.onError(msg.message);
    };
    socket.onclose = (ev) => {
      if (closed) return;
      // 1008 is the server refusing the socket: a missing or wrong token.
      if (ev.code === 1008 && !askedForToken) {
        askedForToken = true;
        const entered = window.prompt(
          "This flow-atelier server requires an API token (ATELIER_API_TOKEN):",
          "",
        );
        if (entered) {
          setApiToken(entered);
          connect();
          return;
        }
      }
      if (ev.code === 1008) {
        handlers.onError(ev.reason || "The server refused the connection.");
        return;
      }
      timer = setTimeout(connect, RETRY_MS[Math.min(attempt++, RETRY_MS.length - 1)]);
    };
  };

  connect();
  return {
    watch: (task) => {
      watched = task;
      sendWatch();
    },
    close: () => {
      closed = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    },
  };
}
