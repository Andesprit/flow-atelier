import type { ToolType } from "@/types/conduit";

/**
 * The one description of every tool. There used to be four copies of this
 * (designer TaskNode, designer ToolPanel, designer Inspector, kanban toolMeta)
 * and they had drifted: `harness:claude-code` rendered purple in a canvas node
 * and terracotta in the palette beside it, `harness:codex` was near-white in
 * two files and pink in the other two, and `tool:hitl` was missing entirely
 * from one of them.
 *
 * Colours are token references, not literals, so each one resolves to a
 * lightness that clears 4.5:1 against whichever theme is active — the old
 * literals were tuned for the dark surface and measured 1.1-2.2:1 on light.
 */
export interface ToolMeta {
  name: ToolType;
  desc: string;
  color: string;
}

export const TOOL_META: ToolMeta[] = [
  {
    name: "tool:bash",
    desc: "Shell command via subprocess",
    color: "var(--color-tool-bash)",
  },
  {
    name: "tool:hitl",
    desc: "Prompt a human for named inputs",
    color: "var(--color-tool-hitl)",
  },
  {
    name: "tool:conduit",
    desc: "Recurse into another conduit",
    color: "var(--color-tool-conduit)",
  },
  {
    name: "harness:claude-code",
    desc: "Inline Claude Code harness",
    color: "var(--color-tool-claude-code)",
  },
  {
    name: "harness:codex",
    desc: "Codex harness",
    color: "var(--color-tool-codex)",
  },
  {
    name: "harness:opencode",
    desc: "opencode harness",
    color: "var(--color-tool-opencode)",
  },
  {
    name: "harness:copilot",
    desc: "Copilot harness",
    color: "var(--color-tool-copilot)",
  },
  {
    name: "harness:cursor",
    desc: "Cursor harness",
    color: "var(--color-tool-cursor)",
  },
  {
    name: "harness:gemini",
    desc: "Gemini CLI harness",
    color: "var(--color-tool-gemini)",
  },
  {
    name: "harness:qwen-code",
    desc: "Qwen Code harness",
    color: "var(--color-tool-qwen-code)",
  },
  {
    name: "harness:goose",
    desc: "goose harness",
    color: "var(--color-tool-goose)",
  },
  {
    name: "harness:amp-acp",
    desc: "Amp harness",
    color: "var(--color-tool-amp-acp)",
  },
  {
    name: "harness:cline",
    desc: "Cline harness",
    color: "var(--color-tool-cline)",
  },
  {
    name: "harness:auggie",
    desc: "Auggie CLI harness",
    color: "var(--color-tool-auggie)",
  },
];

const TOOL_COLOR_BY_NAME: Record<string, string> = Object.fromEntries(
  TOOL_META.map((t) => [t.name, t.color]),
);

/**
 * Colour for a tool, falling back for agents the palette doesn't enumerate.
 *
 * `TOOL_META` is the designer's default palette, not the set of legal tools:
 * a conduit may name any ACP agent the backend has registered (see
 * `atelier harness list`). Those still have to render, so unknown harnesses
 * share one colour rather than resolving to `undefined` and inheriting
 * whatever the surrounding text happens to be.
 */
export function toolColor(tool: ToolType): string {
  const base = tool.startsWith("harness:") ? tool.split(":", 2).join(":") : tool;
  return TOOL_COLOR_BY_NAME[base] ?? "var(--color-tool-harness-other)";
}
