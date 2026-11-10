import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, cleanup } from "@testing-library/react";
import type { ClientWsMessage, ServerWsMessage } from "@/types/ws";

// A stand-in socket the test drives directly: `send` records what the hook
// emitted and can be made to throw, which is the only way to prove the hook
// keeps the prompt pending when the wire is gone.
class FakeSocket {
  static last: FakeSocket | null = null;
  sent: ClientWsMessage[] = [];
  sendImpl: ((msg: ClientWsMessage) => void) | null = null;
  onMessage: (msg: ServerWsMessage) => void = () => {};
  onClose: (code: number, reason: string) => void = () => {};
  readyState = 1;

  constructor() {
    FakeSocket.last = this;
  }

  send(msg: ClientWsMessage) {
    if (this.sendImpl) this.sendImpl(msg);
    this.sent.push(msg);
  }

  close() {}
  waitForOpen() {
    return Promise.resolve();
  }
}

vi.mock("@/services/api/run-conduit", () => ({
  RunConduitSocket: FakeSocket,
}));

const { useConduit, ANSWER_NOT_SENT } = await import("@/hooks/useConduit");

describe("useConduit agent input answers", () => {
  beforeEach(() => {
    FakeSocket.last = null;
  });
  afterEach(cleanup);

  /** Start a run and park two interactive tasks on their own prompts. */
  async function twoPending(onError?: (m: string) => void) {
    const hook = renderHook(() => useConduit({ onError }));
    await act(async () => {
      hook.result.current.run("c", {}, "/p");
    });
    const sock = FakeSocket.last!;
    act(() => {
      sock.onMessage({ type: "started", flowId: "f1" });
      sock.onMessage({
        type: "agent_input_request",
        flowId: "f1",
        requestId: "r-1",
        prompt: "which colour?",
        task: "ask_colour",
      });
      sock.onMessage({
        type: "agent_input_request",
        flowId: "f1",
        requestId: "r-2",
        prompt: "which size?",
        task: "ask_size",
      });
    });
    return { hook, sock };
  }

  it("keeps both live prompts and answers each by its own id", async () => {
    const { hook, sock } = await twoPending();
    expect(hook.result.current.liveRuns[0].agentRequests).toHaveLength(2);

    act(() => {
      hook.result.current.answerAgentInput("f1", "r-2", "large");
    });

    expect(sock.sent).toContainEqual({
      type: "agent_input_answer",
      flowId: "f1",
      requestId: "r-2",
      answer: "large",
    });
    expect(hook.result.current.liveRuns[0].agentRequests).toEqual([
      { flowId: "f1", requestId: "r-1", prompt: "which colour?", taskName: "ask_colour" },
    ]);
  });

  it("keeps the prompt pending and reports the error when the send throws", async () => {
    const onError = vi.fn();
    const { hook, sock } = await twoPending(onError);
    sock.sendImpl = () => {
      throw new Error("cannot send on a closed connection");
    };

    act(() => {
      hook.result.current.answerAgentInput("f1", "r-1", "blue");
    });

    expect(onError).toHaveBeenCalledWith(ANSWER_NOT_SENT);
    expect(
      hook.result.current.liveRuns[0].agentRequests.map((r) => r.requestId),
    ).toEqual(["r-1", "r-2"]);
  });

  it("keeps the prompt pending and reports the error when there is no socket", () => {
    const onError = vi.fn();
    const hook = renderHook(() => useConduit({ onError }));

    act(() => {
      hook.result.current.answerAgentInput("f1", "r-1", "blue");
    });

    expect(onError).toHaveBeenCalled();
    expect(FakeSocket.last).toBeNull();
  });
});
