import { describe, it, expect } from "vitest";
import { renderConduitYaml } from "@/utils/yaml";
import type { Conduit } from "@/types/conduit";

const minimalConduit: Conduit = {
  name: "test_flow",
  description: "A test conduit",
  inputs: {},
  tasks: [],
};

const fullConduit: Conduit = {
  name: "deploy",
  description: "Deploy the service",
  timeout: 300,
  maxConcurrency: 2,
  inputs: { service: "service slug", ref: "git ref" },
  tasks: [
    {
      name: "build",
      tool: "tool:bash",
      description: "Build",
      task: "bun run build",
      dependsOn: [],
    },
    {
      name: "review",
      tool: "harness:claude-code",
      description: "Review code",
      task: "Review the diff",
      dependsOn: ["build"],
      conditions: { "build": { kind: "match", pattern: "ok" } },
    },
    {
      name: "retry_step",
      tool: "tool:bash",
      description: "Retry",
      task: "run with retry",
      dependsOn: [],
      repeat: 5,
      interactive: true,
      inputs: { env: "staging" },
    },
  ],
};

describe("renderConduitYaml", () => {
  it("renders a minimal conduit with name and empty inputs/tasks", () => {
    const result = renderConduitYaml(minimalConduit);
    expect(result).toContain("name: test_flow");
    expect(result).toContain("description: \"A test conduit\"");
    expect(result).toContain("inputs:");
    expect(result).toContain("tasks:");
  });

  it("renders optional fields (timeout, maxConcurrency)", () => {
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("timeout: 300");
    expect(result).toContain("max_concurrency: 2");
  });

  it("renders conduit inputs as key-value pairs", () => {
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("  service: \"service slug\"");
    expect(result).toContain("  ref: \"git ref\"");
  });

  it("renders task name and tool", () => {
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("- name: build");
    expect(result).toContain("tool: tool:bash");
  });

  it("renders depends_on when task has dependencies", () => {
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("depends_on: [\"build.output.match(ok)\"]");
  });

  it("omits depends_on when task has no dependencies", () => {
    const result = renderConduitYaml(minimalConduit);
    expect(result).not.toContain("depends_on:");
  });

  it("encodes a condition into the depends_on entry, not a conditional_on key", () => {
    // `conditional_on` is not a field in the conduit schema — the engine reads
    // conditions out of the depends_on DSL, so emitting one produced a preview
    // the engine would silently ignore.
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("build.output.match(ok)");
    expect(result).not.toContain("conditional_on");
  });

  it("renders repeat when present", () => {
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("repeat: 5");
  });

  it("renders interactive when true", () => {
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("interactive: true");
  });

  it("renders task-level inputs when present", () => {
    const result = renderConduitYaml(fullConduit);
    expect(result).toContain("env: \"staging\"");
  });

  it("quotes names/keys containing YAML-special punctuation", () => {
    const conduit: Conduit = {
      name: "build: prod",
      description: "x",
      inputs: { "time #1": "a timestamp" },
      tasks: [
        {
          name: "step: one",
          tool: "tool:bash",
          description: "d",
          task: "echo hi",
          dependsOn: [],
          inputs: { "key #2": "v" },
        },
      ],
    };
    const result = renderConduitYaml(conduit);
    // Special names/keys are double-quoted...
    expect(result).toContain('name: "build: prod"');
    expect(result).toContain('"time #1": "a timestamp"');
    expect(result).toContain('- name: "step: one"');
    expect(result).toContain('"key #2": "v"');
    // ...and the unquoted-but-broken forms are gone.
    expect(result).not.toContain("name: build: prod");
    expect(result).not.toContain("time #1: ");
  });
});


it("exports the conduit interaction policy including supervisor limits", () => {
  const result = renderConduitYaml({
    ...minimalConduit,
    interaction: {
      questions: "hybrid", permissions: "human",
      supervisor: { tool: "harness:codex", instructions: "Ask about scope\nKeep API stable",
        maxReplies: 4, timeout: 60, maxContextChars: 90000 },
    },
  });
  expect(result).toContain("interaction:\n  questions: hybrid\n  permissions: human");
  expect(result).toContain("    max_replies: 4");
  expect(result).toContain("    max_context_chars: 90000");
  expect(result).toContain(JSON.stringify("Ask about scope\nKeep API stable"));
  expect(renderConduitYaml(minimalConduit)).not.toContain("interaction:");
});
