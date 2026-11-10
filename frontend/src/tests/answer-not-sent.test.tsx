import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, render, screen, fireEvent, act, cleanup } from "@testing-library/react";
import type { ClientWsMessage, ServerWsMessage } from "@/types/ws";
import type { HitlRequest } from "@/types/task";

// Each fake stands for one execution session; the test moves it through
// readyState values and can make send throw.
class FakeSocket {
  static all: FakeSocket[] = [];
  sent: ClientWsMessage[] = [];
  throwOnSend = false;
  onMessage: (msg: ServerWsMessage) => void = () => {};
  onClose: (code: number, reason: string) => void = () => {};
  readyState = 1;
  constructor() {
    FakeSocket.all.push(this);
  }
  send(msg: ClientWsMessage) {
    if (this.throwOnSend || this.readyState > 1) throw new Error("cannot send on a closed connection");
    this.sent.push(msg);
  }
  close() {}
  waitForOpen() {
    return Promise.resolve();
  }
}

vi.mock("@/services/api/run-conduit", () => ({ RunConduitSocket: FakeSocket }));

const { useConduit, ANSWER_NOT_SENT } = await import("@/hooks/useConduit");
const { HitlSection } = await import("@/components/FlowDrawer");

afterEach(() => {
  cleanup();
  FakeSocket.all = [];
});

const answers = (s: FakeSocket) => s.sent.filter((m) => m.type !== "run");

/** One run on one session, parked on a gate, a nested gate and two agent turns. */
async function waiting() {
  const onError = vi.fn();
  const hook = renderHook(() => useConduit({ onError }));
  await act(async () => {
    hook.result.current.run("c", {}, "/p");
  });
  const sock = FakeSocket.all[0];
  act(() => {
    sock.onMessage({ type: "started", flowId: "f1" });
    sock.onMessage({ type: "hitl_request", flowId: "f1", task: "gate", inputs: [] } as ServerWsMessage);
    sock.onMessage({ type: "agent_input_request", flowId: "f1", requestId: "r-1", prompt: "a?", task: "t1" });
    sock.onMessage({ type: "agent_input_request", flowId: "child", requestId: "r-2", prompt: "b?", task: "t2" });
  });
  const run = () => hook.result.current.liveRuns.find((r) => r.flowId === "f1")!;
  return { hook, sock, onError, run };
}

/** The first session drops and a second one opens, optionally for the same flow id. */
async function replaced(sameFlowId: boolean) {
  const w = await waiting();
  act(() => {
    w.sock.readyState = 3;
    w.sock.onClose(1006, "");
  });
  await act(async () => {
    w.hook.result.current.run("c", {}, "/p");
  });
  const next = FakeSocket.all[1];
  act(() => {
    next.onMessage({ type: "started", flowId: sameFlowId ? "f1" : "f2" });
  });
  return { ...w, next };
}

describe("answers go only through the session that asked", () => {
  it("sends each answer once on the open original session and clears only that question", async () => {
    const { hook, sock, onError, run } = await waiting();
    act(() => hook.result.current.answerAgentInput("child", "r-2", "large"));
    act(() => hook.result.current.answerHITL("f1", { response: "yes" }));
    expect(answers(sock)).toEqual([
      { type: "agent_input_answer", flowId: "child", requestId: "r-2", answer: "large" },
      { type: "hitl_answer", flowId: "f1", answers: { response: "yes" } },
    ]);
    expect(onError).not.toHaveBeenCalled();
    expect(run().hitlRequest).toBeUndefined();
    expect(run().agentRequests.map((r) => r.requestId)).toEqual(["r-1"]);
  });

  it.each([
    ["connecting", 0],
    ["closing", 2],
    ["closed", 3],
  ])("refuses on a %s session and keeps every question", async (_, state) => {
    const { hook, sock, onError, run } = await waiting();
    sock.readyState = state;
    act(() => hook.result.current.answerHITL("f1", { response: "secret" }));
    act(() => hook.result.current.answerAgentInput("f1", "r-1", "secret"));
    expect(answers(sock)).toEqual([]);
    expect(onError).toHaveBeenCalledTimes(2);
    expect(onError).toHaveBeenCalledWith(ANSWER_NOT_SENT);
    expect(ANSWER_NOT_SENT).not.toContain("secret");
    expect(run().hitlRequest).toBeDefined();
    expect(run().agentRequests.map((r) => r.requestId)).toEqual(["r-1"]);
  });

  it("refuses when the session is gone or its send throws", async () => {
    const { hook, sock, onError, run } = await waiting();
    sock.throwOnSend = true;
    act(() => hook.result.current.answerHITL("f1", { response: "x" }));
    act(() => {
      sock.readyState = 3;
      sock.onClose(1006, "");
    });
    act(() => hook.result.current.answerAgentInput("f1", "r-1", "x"));
    expect(onError).toHaveBeenNthCalledWith(1, ANSWER_NOT_SENT);
    expect(onError).toHaveBeenNthCalledWith(2, ANSWER_NOT_SENT);
    expect(run().hitlRequest).toBeDefined();
    expect(run().agentRequests.map((r) => r.requestId)).toEqual(["r-1"]);
  });

  it.each([
    ["a different flow id", false],
    ["the same flow id", true],
  ])("never sends an old question's answer on a replacement session for %s", async (_, same) => {
    const { hook, next, onError } = await replaced(same);
    act(() => hook.result.current.answerHITL("f1", { response: "x" }));
    act(() => hook.result.current.answerAgentInput("f1", "r-1", "x"));
    act(() => hook.result.current.answerAgentInput("child", "r-2", "x"));
    expect(answers(next)).toEqual([]);
    expect(onError).toHaveBeenCalledTimes(3);
  });

  it("answers a question the replacement session asked itself", async () => {
    const { hook, next, onError } = await replaced(true);
    act(() => next.onMessage({ type: "hitl_request", flowId: "f1", task: "gate", inputs: [] } as ServerWsMessage));
    act(() => hook.result.current.answerHITL("f1", { response: "ok" }));
    expect(answers(next)).toEqual([{ type: "hitl_answer", flowId: "f1", answers: { response: "ok" } }]);
    expect(onError).not.toHaveBeenCalled();
  });
});

describe("HITL form draft", () => {
  const question = (key: string): HitlRequest => ({ fromTool: "tool:hitl", comment: "ok?", taskName: "gate", key });

  it("keeps the typed text after submit and starts empty for a new question", () => {
    const onRespond = vi.fn();
    const view = render(<HitlSection hitl={question("q1")} onRespond={onRespond} />);
    const field = () => screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(field(), { target: { value: "ship it" } });
    fireEvent.click(screen.getByRole("button", { name: "resume" }));
    expect(onRespond).toHaveBeenCalledWith({ response: "ship it" });
    view.rerender(<HitlSection hitl={question("q1")} onRespond={onRespond} />);
    expect(field().value).toBe("ship it");
    view.rerender(<HitlSection hitl={question("q2")} onRespond={onRespond} />);
    expect(field().value).toBe("");
  });

  it("still rejects an empty answer", () => {
    const onRespond = vi.fn();
    render(<HitlSection hitl={question("q1")} onRespond={onRespond} />);
    fireEvent.click(screen.getByRole("button", { name: "resume" }));
    expect(onRespond).not.toHaveBeenCalled();
    expect(screen.getByText("response is required")).toBeTruthy();
  });
});
