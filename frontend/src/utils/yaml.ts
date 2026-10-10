import type { Conduit, ConduitTask } from "@/types/conduit";
import { formatDependency } from "@/utils/conditions";

// Plain scalars/keys that are safe to emit unquoted. Anything with YAML-special
// punctuation (`:`, `#`, etc.) is double-quoted via JSON.stringify so a name
// like `build: prod` or a field named `time #1` still produces parseable YAML.
const SAFE_SCALAR = /^[A-Za-z0-9_][A-Za-z0-9 ._-]*$/;

function yamlScalar(s: string): string {
  return SAFE_SCALAR.test(s) ? s : JSON.stringify(s);
}

function appendTask(lines: string[], t: ConduitTask, indent: number): void {
  const list = " ".repeat(indent);
  const field = " ".repeat(indent + 2);
  lines.push(`${list}- name: ${yamlScalar(t.name)}`);
  lines.push(`${field}tool: ${t.tool}`);
  lines.push(`${field}description: ${JSON.stringify(t.description)}`);
  if (!t.tasks) lines.push(`${field}task: ${JSON.stringify(t.task)}`);
  if (t.dependsOn.length > 0) {
    const deps = t.dependsOn.map((dep) =>
      yamlScalar(formatDependency(dep, t.conditions?.[dep])),
    );
    lines.push(`${field}depends_on: [${deps.join(", ")}]`);
  }
  if (t.repeat) lines.push(`${field}repeat: ${t.repeat}`);
  if (t.until != null) lines.push(`${field}until: ${JSON.stringify(t.until)}`);
  if (t.while != null) lines.push(`${field}while: ${JSON.stringify(t.while)}`);
  if (t.onExhaust && t.onExhaust !== "complete") lines.push(`${field}on_exhaust: ${t.onExhaust}`);
  if (t.interactive) lines.push(`${field}interactive: true`);
  if (t.inputs && Object.keys(t.inputs).length > 0) {
    lines.push(`${field}inputs:`);
    for (const [key, val] of Object.entries(t.inputs)) {
      lines.push(`${field}  ${yamlScalar(key)}: ${JSON.stringify(val)}`);
    }
  }
  if (t.tasks) {
    lines.push(`${field}tasks:`);
    for (const child of t.tasks) appendTask(lines, child, indent + 4);
  }
}

export function renderConduitYaml(c: Conduit): string {
  const lines: string[] = [];
  lines.push(`name: ${yamlScalar(c.name)}`);
  lines.push(`description: ${JSON.stringify(c.description)}`);
  if (c.timeout) lines.push(`timeout: ${c.timeout}`);
  if (c.maxConcurrency) lines.push(`max_concurrency: ${c.maxConcurrency}`);
  if (c.interaction) {
    lines.push("interaction:");
    lines.push(`  questions: ${c.interaction.questions}`);
    lines.push(`  permissions: ${c.interaction.permissions}`);
    if (c.interaction.supervisor) {
      const supervisor = c.interaction.supervisor;
      lines.push("  supervisor:");
      lines.push(`    tool: ${yamlScalar(supervisor.tool)}`);
      if (supervisor.instructions) lines.push(`    instructions: ${JSON.stringify(supervisor.instructions)}`);
      if (supervisor.maxReplies !== undefined) lines.push(`    max_replies: ${supervisor.maxReplies}`);
      if (supervisor.timeout !== undefined) lines.push(`    timeout: ${supervisor.timeout}`);
      if (supervisor.maxContextChars !== undefined) lines.push(`    max_context_chars: ${supervisor.maxContextChars}`);
    }
  }
  lines.push(`inputs:`);
  for (const [key, hint] of Object.entries(c.inputs)) {
    lines.push(`  ${yamlScalar(key)}: ${JSON.stringify(hint)}`);
  }
  lines.push(`tasks:`);
  for (const t of c.tasks) appendTask(lines, t, 2);
  return lines.join("\n");
}
