import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { BASE_URL, fetchJson } from "@/services/client";

const url = BASE_URL + "/chat";
const storageKey = "atelier:chat:" + BASE_URL;
type Turn = { requestId: string; text: string; answer: string; status: string; result?: string };
type Chat = {
  id: string; harness: string; turns: Turn[]; revision: number; busy: boolean; executing: boolean; error?: string;
  pending?: { id: string; details: string };
};

function newRequestId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
}

export function ChatPanel() {
  const [id, setId] = useState(() => sessionStorage.getItem(storageKey));
  const [open, setOpen] = useState(Boolean(id));
  const [harnesses, setHarnesses] = useState<string[] | null>(null);
  const [selected, setSelected] = useState("");
  const [chat, setChat] = useState<Chat>();
  const [sources, setSources] = useState<Array<{
    id: string; provider: string; session: string; project?: string; date?: string; error?: string; source: string;
  }> | null>(null);
  const [source, setSource] = useState("");
  const [transcript, setTranscript] = useState("");
  const [draft, setDraft] = useState("");
  const [requestId, setRequestId] = useState(() => newRequestId());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);

  function update(state: Chat) {
    setChat(prev => prev?.id === state.id && prev.revision > state.revision ? prev : state);
  }

  useEffect(() => {
    if (!open) return;
    let active = true;
    fetchJson<string[]>(url + "/harnesses", undefined, { method: "GET" }).then(items => {
      if (active) {
        setHarnesses(items);
        setSelected(prev => prev || items[0] || "");
      }
    }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [open, retry]);

  useEffect(() => {
    if (!open || !id) return;
    let active = true;
    async function follow() {
      let revision = -1;
      while (active) {
        const state = await fetchJson<Chat>(url + "/" + id + "?after=" + revision, undefined, { method: "GET" });
        if (!active) return;
        update(state);
        revision = state.revision;
        if (!state.busy && !state.executing) return;
      }
    }
    follow().catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [open, id, chat?.busy, chat?.executing, retry]);

  async function action(work: () => Promise<void>) {
    setError("");
    setPending(true);
    try { await work(); }
    catch (e) { setError(String(e)); }
    finally { setPending(false); }
  }

  function fresh() {
    void action(async () => {
      if (id) await fetchJson(url + "/" + id, undefined, { method: "DELETE" }).catch(e => {
        if (!String(e).includes("410")) throw e;
      });
      sessionStorage.removeItem(storageKey);
      setId(null);
      setChat(undefined);
      setDraft("");
      setSource("");
      setTranscript("");
      setRequestId(newRequestId());
    });
  }

  function send() {
    void action(async () => {
      let target = id;
      if (!target) {
        const state = await fetchJson<Chat>(url, { harness: selected });
        sessionStorage.setItem(storageKey, state.id);
        setId(state.id);
        target = state.id;
      }
      update(await fetchJson<Chat>(url + "/" + target + "/turn", { requestId, text: draft, source: source || null }));
      setDraft("");
      setSource("");
      setTranscript("");
      setRequestId(newRequestId());
    });
  }

  return <section className="mx-auto mt-8 max-w-[1280px] border-t border-border pt-4">
    <Button variant="outline" aria-expanded={open} onClick={() => setOpen(!open)}>Orchestrator chat</Button>
    {open && <div className="mt-4 grid max-w-3xl gap-3">
      <label className="text-sm">Harness{" "}
        <select aria-label="Chat harness" className="rounded-sm border border-border bg-background p-2"
          value={chat?.harness || selected} disabled={!!id || pending} onChange={e => setSelected(e.target.value)}>
          {harnesses?.map(h => <option key={h}>{h}</option>)}
        </select>
      </label>
      {harnesses?.length === 0 && <p className="text-sm">Chat controls require macOS or Linux with bubblewrap.
        Install an adapter with <code>atelier harness install claude-acp</code> or <code>atelier harness install codex-acp</code>.</p>}
      <div>
        <Button variant="ghost" disabled={pending} onClick={() => void action(async () => {
          setSources(await fetchJson(url + "/history", undefined, { method: "GET" }));
        })}>Browse local history</Button>
        {sources && <label className="mt-2 grid gap-1 text-sm">Session to include with this message
          <select className="max-w-full rounded-sm border border-border bg-background p-2" value={source}
            onChange={e => { setSource(e.target.value); setTranscript(""); setRequestId(newRequestId()); }}>
            <option value="">No session</option>
            {sources.map(s => <option value={s.id} key={s.id} disabled={!!s.error}>
              {[s.provider, s.project, s.date, s.session, s.source, s.error].filter(Boolean).join(" · ")}
            </option>)}
          </select>
        </label>}
        {source && <Button variant="link" disabled={pending} onClick={() => void action(async () => {
          const data = await fetchJson<{ messages: unknown[]; error?: string; warning?: string }>(
            url + "/history?source=" + encodeURIComponent(source), undefined, { method: "GET" });
          setTranscript([data.error, data.warning, JSON.stringify(data.messages, null, 2)].filter(Boolean).join("\n"));
        })}>Read session</Button>}
        {transcript && <pre className="max-h-64 overflow-auto whitespace-pre-wrap">{transcript}</pre>}
      </div>
      {chat?.turns.map(t => <div key={t.requestId} className="grid gap-1 border-l border-border pl-3">
        <p className="whitespace-pre-wrap break-words"><strong>You</strong> {t.text}</p>
        <p className="whitespace-pre-wrap break-words"><strong>Agent</strong> {t.answer}</p>
        {t.result && <pre className="max-h-64 overflow-auto whitespace-pre-wrap">{t.result}</pre>}
        {t.status === "cancelled" && <span>Turn cancelled</span>}
      </div>)}
      {chat?.pending && <section aria-label="Review action" className="rounded-sm border border-border p-3">
        <h3>Review action</h3>
        <pre className="my-2 max-h-72 overflow-auto whitespace-pre-wrap">{chat.pending.details}</pre>
        <Button disabled={pending || chat.busy} onClick={() => void action(async () => {
          update(await fetchJson<Chat>(url + "/" + id + "/confirm/" + chat.pending!.id, {}));
        })}>Confirm action</Button>
      </section>}
      {(error || chat?.error) && <div role="alert">{error || chat?.error}{" "}
        {error && <Button variant="link" onClick={() => { setError(""); setRetry(n => n + 1); }}>Reconnect</Button>}
      </div>}
      <label className="grid gap-1 text-sm">Message
        <textarea className="rounded-sm border border-border bg-background p-2" value={draft} maxLength={64000}
          onChange={e => { setDraft(e.target.value); setRequestId(newRequestId()); }} />
      </label>
      <div className="flex flex-wrap gap-2">
        <Button disabled={pending || !draft.trim() || !selected || !!chat?.busy || !!chat?.error || (!!id && !chat)}
          onClick={send}>Send</Button>
        {chat?.busy && <Button variant="outline" disabled={pending}
          onClick={() => void action(async () => { update(await fetchJson<Chat>(url + "/" + id + "/cancel", {})); })}>
          Cancel turn
        </Button>}
        {id && <Button variant="ghost" disabled={pending} onClick={fresh}>Fresh chat</Button>}
      </div>
    </div>}
  </section>;
}
