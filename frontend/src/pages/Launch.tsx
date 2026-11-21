import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { BASE_URL, fetchJson } from "@/services/client";
import { inputDefault, inputHint, submittedInputs, type Conduit } from "@/types/conduit";

const selectStyle = "block h-11 w-full rounded-sm border border-border bg-background px-3 text-base";
const get = { method: "GET" };

export default function Launch() {
  const [projects, setProjects] = useState<string[]>([]);
  const [folder, setFolder] = useState("");
  const [project, setProject] = useState("");
  const [conduits, setConduits] = useState<Conduit[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const navigate = useNavigate();
  const conduit = conduits.find((item) => item.name === selected);

  useEffect(() => {
    let active = true;
    fetchJson<string[]>(`${BASE_URL}/mobile/projects`, undefined, get).then((paths) => {
      if (!active) return;
      setProjects(paths);
      setProject(paths[0]);
      setFolder(paths[0]);
    }).catch((e) => { if (active) { setError(e.message); setLoading(false); } });
    const update = () => setOnline(navigator.onLine);
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      active = false;
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  useEffect(() => {
    if (!project) return;
    let active = true;
    setLoading(true);
    setError("");
    fetchJson<Conduit[]>(`${BASE_URL}/mobile/conduits?project=${encodeURIComponent(project)}`,
      undefined, get).then((items) => {
      if (!active) return;
      setConduits(items);
      setSelected(items[0]?.name ?? "");
    }).catch((e) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [project]);

  function chooseProject(path: string) {
    setConduits([]);
    setSelected("");
    setFolder(path);
    setProject(path);
    setError("");
  }

  async function launch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!conduit || !online || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    const fields = new FormData(event.currentTarget);
    const values = Object.fromEntries(Object.keys(conduit.inputs).map((name) => [name, String(fields.get(name) ?? "")]));
    try {
      const result = await fetchJson<{ flowId: string }>(`${BASE_URL}/mobile/runs`, {
        conduitName: conduit.name, runPath: project, inputs: submittedInputs(conduit, values),
      });
      navigate(`/runs/${encodeURIComponent(result.flowId)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Launch failed");
      submitting.current = false;
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto grid max-w-xl gap-5 px-4 py-6">
      <h1 className="font-display text-panel">Launch a conduit</h1>
      <label>Project folder
        <select className={selectStyle} disabled={busy || !projects.length}
          value={project} onChange={(e) => chooseProject(e.target.value)}>
          {projects.map((path) => <option key={path}>{path}</option>)}
          <option value="">Another folder…</option>
        </select>
      </label>
      {!project && projects.length > 0 && (
        <form className="flex items-end gap-2" onSubmit={(e) => {
          e.preventDefault();
          chooseProject(folder.trim());
          setProjects((paths) => [...new Set([...paths, folder.trim()])]);
        }}>
          <label className="min-w-0 flex-1">Folder on this server
            <Input required value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="/path/to/project" />
          </label>
          <Button variant="outline">Use folder</Button>
        </form>
      )}
      {!online && <p role="status">Offline. Reconnect to launch a run.</p>}
      {error && <p role="alert" className="break-words text-destructive">{error}</p>}
      {loading && <p role="status">Loading projects and conduits…</p>}
      {project && !loading && !error && !conduits.length && <p>No conduits in this project.</p>}
      {conduits.length > 0 && (
        <label>Conduit
          <select className={selectStyle} value={selected} disabled={busy} onChange={(e) => setSelected(e.target.value)}>
            {conduits.map((item) => <option key={item.name}>{item.name}</option>)}
          </select>
        </label>
      )}
      {conduit && (
        <form key={project + "/" + conduit.name} onSubmit={launch} className="grid gap-4">
          {conduit.description && <p className="text-muted-foreground">{conduit.description}</p>}
          {Object.entries(conduit.inputs).map(([name, hint]) => (
            <label key={name}>{name}
              <Input name={name} required={inputDefault(hint) == null} placeholder={inputHint(hint)} />
            </label>
          ))}
          <Button type="submit" disabled={!online || busy}>{busy ? "Launching…" : "Launch"}</Button>
        </form>
      )}
    </main>
  );
}

