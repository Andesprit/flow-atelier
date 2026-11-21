// A sandbox probe shaped like the installed ACP adapters; it never opens a network connection.
import { appendFileSync, readFileSync } from "node:fs";
import { createInterface } from "node:readline";

const outside = new URL("./outside.txt", import.meta.url);
let restricted;
for await (const line of createInterface({ input: process.stdin })) {
  const { id, method, params } = JSON.parse(line);
  let result;
  if (method === "initialize") {
    result = { protocolVersion: 1, agentCapabilities: {}, authMethods: [] };
  } else if (method === "session/new") {
    const options = params._meta?.claudeCode?.options;
    const codex = JSON.parse(process.env.CODEX_CONFIG || "{}");
    restricted = params.mcpServers.length === 0 && (options?.tools?.length === 0 && options.strictMcpConfig
      || codex.features?.shell_tool === false && codex.sandbox_mode === "read-only");
    result = { sessionId: "probe", modes: {currentModeId: "read-only",
      availableModes: [{id: "read-only", name: "Read only"}]} };
  } else if (method === "session/prompt") {
    const target = readFileSync(outside, "utf8");
    let read = false, write = false;
    try { readFileSync(target); read = true; } catch {}
    try { appendFileSync(target, "changed"); write = true; } catch {}
    console.log(JSON.stringify({ jsonrpc: "2.0", method: "session/update", params: {
      sessionId: "probe", update: { sessionUpdate: "agent_message_chunk",
        content: { type: "text", text: JSON.stringify({restricted, read, write}) } }
    }}));
    result = { stopReason: "end_turn" };
  }
  if (id != null) console.log(JSON.stringify({ jsonrpc: "2.0", id, result }));
}
