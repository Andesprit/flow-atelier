import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { BASE_URL, fetchJson } from "@/services/client";

const url = BASE_URL + "/chat";
const storageKey = "atelier:chat:" + BASE_URL;
type Turn = { requestId: string; text: string; answer: string; status: string };
type Chat = {
  id: string; harness: string; turns: Turn[]; revision: number; busy: boolean; error?: string;
};

export function ChatPanel() {
  const [id, setId] = useState(() => sessionStorage.getItem(storageKey));
  const [open, setOpen] = useState(Boolean(id));
  const [harnesses, setHarnesses] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [chat, setChat] = useState<Chat>();
  const [draft, setDraft] = useState("");
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
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
        if (!state.busy) return;
      }
    }
    follow().catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [open, id, chat?.busy, retry]);

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
      setRequestId(crypto.randomUUID());
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
      update(await fetchJson<Chat>(url + "/" + target + "/turn", { requestId, text: draft }));
      setDraft("");
      setRequestId(crypto.randomUUID());
    });
  }

  return <section className="mx-auto mt-8 max-w-[1280px] border-t border-border pt-4">
    <Button variant="outline" aria-expanded={open} onClick={() => setOpen(!open)}>Orchestrator chat</Button>
    {open && <div className="mt-4 grid max-w-3xl gap-3">
      <label className="text-sm">Harness{" "}
        <select aria-label="Chat harness" className="rounded-sm border border-border bg-background p-2"
          value={chat?.harness || selected} disabled={!!id || pending} onChange={e => setSelected(e.target.value)}>
          {harnesses.map(h => <option key={h}>{h}</option>)}
        </select>
      </label>
      {chat?.turns.map(t => <div key={t.requestId} className="grid gap-1 border-l border-border pl-3">
        <p className="whitespace-pre-wrap break-words"><strong>You</strong> {t.text}</p>
        <p className="whitespace-pre-wrap break-words"><strong>Agent</strong> {t.answer}</p>
        {t.status === "cancelled" && <span>Turn cancelled</span>}
      </div>)}
      {(error || chat?.error) && <div role="alert">{error || chat?.error}{" "}
        {error && <Button variant="link" onClick={() => { setError(""); setRetry(n => n + 1); }}>Reconnect</Button>}
      </div>}
      <label className="grid gap-1 text-sm">Message
        <textarea className="rounded-sm border border-border bg-background p-2" value={draft} maxLength={64000}
          onChange={e => { setDraft(e.target.value); setRequestId(crypto.randomUUID()); }} />
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
