import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { BASE_URL, fetchJson } from "@/services/client";

export default function PushNotice() {
  const { token } = useParams();
  const navigate = useNavigate();
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    fetchJson<{ flowId: string }>(`${BASE_URL}/push/runs/${encodeURIComponent(token ?? "")}`,
      undefined, { method: "GET" }).then(({ flowId }) => {
      if (active) navigate(`/runs/${encodeURIComponent(flowId)}`, { replace: true });
    }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [navigate, token]);
  return <p role="status" className="p-4">{error || "Opening run…"}</p>;
}

