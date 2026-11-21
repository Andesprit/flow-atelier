import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ChatPanel } from "@/features/dashboard/components/chat-panel";
import { BASE_URL, fetchJson } from "@/services/client";

vi.mock("@/services/client", () => ({ BASE_URL: "http://localhost", fetchJson: vi.fn() }));

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.resetAllMocks();
});

it("reattaches to the selected harness and waits for a click on the exact preview", async () => {
  const details = JSON.stringify({
    op: "enable", folder: "/repo", conduit: "daily", inputs: { repo_name: "sample" }, schedule: { mode: "interval", every_minutes: 5 },
  }, null, 2);
  const snapshot = {
    id: "chat", harness: "harness:two", revision: 3, busy: false, executing: false,
    turns: [{ requestId: "r", text: "Schedule it", answer: "Ready", status: "end_turn" }],
    pending: { id: "approval", details },
  };
  sessionStorage.setItem("atelier:chat:" + BASE_URL, "chat");
  vi.mocked(fetchJson).mockImplementation(async url => {
    if (url.endsWith("/harnesses")) return ["harness:one", "harness:two"];
    if (url.endsWith("/confirm/approval")) return { ...snapshot, revision: 4, pending: null };
    return snapshot;
  });
  render(<ChatPanel />);
  const click = await screen.findByRole("button", { name: "Confirm action" });
  expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Chat harness" }).value).toBe("harness:two");
  expect(screen.getByRole("region", { name: "Review action" }).textContent).toContain(details);
  expect(vi.mocked(fetchJson).mock.calls.some(([url]) => /\/turn|\/confirm\//.test(url))).toBe(false);
  fireEvent.click(click);
  await waitFor(() => expect(screen.queryByRole("button", { name: "Confirm action" })).toBeNull());
  expect(vi.mocked(fetchJson).mock.calls.filter(([url]) => url.endsWith("/confirm/approval"))).toHaveLength(1);
});

it("reloads mid-turn without sending it again and renders the remaining reply", async () => {
  let finish!: () => void;
  const stream = new Promise<void>(resolve => { finish = resolve; });
  const turn = { requestId: "r", text: "question", answer: "partial", status: "running" };
  const live = { id: "s", harness: "harness:one", revision: 1, busy: true, turns: [turn] };
  sessionStorage.setItem("atelier:chat:" + BASE_URL, "s");
  vi.mocked(fetchJson).mockImplementation(async url => {
    if (url.endsWith("/harnesses")) return ["harness:one"];
    if (url.endsWith("after=-1")) return live;
    await stream;
    return { ...live, busy: false, revision: 2, turns: [{ ...turn, answer: "full answer", status: "end_turn" }] };
  });
  const first = render(<ChatPanel />);
  await screen.findByText("partial");
  first.unmount();
  render(<ChatPanel />);
  await screen.findByRole("button", { name: "Cancel turn" });
  live.busy = false;
  turn.answer = "full answer";
  finish();
  await screen.findByText("full answer");
  expect(vi.mocked(fetchJson).mock.calls.every(([, , options]) => options?.method === "GET")).toBe(true);
});
