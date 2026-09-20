import { describe, it, expect } from "vitest";
import {
  parseDependency,
  formatDependency,
  fromWireTask,
  toWireTask,
  withoutCondition,
  renameConditionSource,
  stripSurroundingQuotes,
} from "@/utils/conditions";
import type { ConduitTask } from "@/types/conduit";

function task(dependsOn: string[], conditions?: ConduitTask["conditions"]): ConduitTask {
  return {
    name: "approve",
    tool: "tool:hitl",
    description: "gate",
    task: "confirm",
    dependsOn,
    ...(conditions ? { conditions } : {}),
  };
}

describe("parseDependency", () => {
  it("returns a plain name unchanged", () => {
    expect(parseDependency("run_tests")).toEqual({ task: "run_tests" });
  });

  it("splits a match condition off the task name", () => {
    expect(parseDependency("code_review.output.match(VERDICT:\\s*APPROVE)")).toEqual({
      task: "code_review",
      condition: { kind: "match", pattern: "VERDICT:\\s*APPROVE" },
    });
  });

  it("splits a not_match condition", () => {
    expect(parseDependency("review.output.not_match(REJECT)")).toEqual({
      task: "review",
      condition: { kind: "not_match", pattern: "REJECT" },
    });
  });

  it("strips quotes the engine would strip", () => {
    expect(parseDependency('a.output.match("PASS")').condition?.pattern).toBe("PASS");
  });

  it("keeps a malformed entry verbatim rather than dropping it", () => {
    expect(parseDependency("a.output.match(unclosed")).toEqual({
      task: "a.output.match(unclosed",
    });
  });
});

describe("formatDependency", () => {
  it("emits a bare name with no condition", () => {
    expect(formatDependency("build")).toBe("build");
  });

  it("emits the match DSL", () => {
    expect(formatDependency("build", { kind: "match", pattern: "ok" })).toBe(
      "build.output.match(ok)",
    );
  });

  it("emits the not_match DSL", () => {
    expect(formatDependency("build", { kind: "not_match", pattern: "ok" })).toBe(
      "build.output.not_match(ok)",
    );
  });

  it("restores the quote characters the pattern was parsed with", () => {
    expect(
      formatDependency("build", { kind: "match", pattern: "ok", quote: '"' }),
    ).toBe('build.output.match("ok")');
    expect(
      formatDependency("build", { kind: "match", pattern: "ok", quote: "'" }),
    ).toBe("build.output.match('ok')");
  });
});

describe("quoted patterns round trip", () => {
  // Opening a conduit in the designer and saving it must be a no-op on disk.
  // The engine strips quotes before compiling, so the UI shows the bare
  // pattern -- but dropping them on save rewrites YAML the user never edited.
  it.each([
    'code_review.output.match("VERDICT: APPROVE")',
    "code_review.output.match('VERDICT: APPROVE')",
    'build.output.not_match("FAIL")',
    "build.output.match(NOQUOTES)",
  ])("preserves %s verbatim", (dep) => {
    const { task: source, condition } = parseDependency(dep);
    expect(formatDependency(source, condition)).toBe(dep);
  });

  it("still exposes the bare pattern to the inspector", () => {
    const { condition } = parseDependency('t.output.match("PASS")');
    expect(condition?.pattern).toBe("PASS");
    expect(condition?.quote).toBe('"');
  });

  it("survives a full wire round trip with quotes intact", () => {
    const wire = task(['code_review.output.match("VERDICT:\\s*APPROVE")']);
    expect(toWireTask(fromWireTask(wire)).dependsOn).toEqual(wire.dependsOn);
  });
});

describe("wire round trip", () => {
  it("survives decode then encode unchanged", () => {
    const wire = task(["run_tests", "code_review.output.match(VERDICT:\\s*APPROVE)"]);
    expect(toWireTask(fromWireTask(wire)).dependsOn).toEqual(wire.dependsOn);
  });

  it("preserves several conditions on one task", () => {
    const wire = task(["a.output.match(X)", "b.output.not_match(Y)", "c"]);
    const model = fromWireTask(wire);

    expect(model.dependsOn).toEqual(["a", "b", "c"]);
    expect(model.conditions).toEqual({
      a: { kind: "match", pattern: "X" },
      b: { kind: "not_match", pattern: "Y" },
    });
    expect(toWireTask(model).dependsOn).toEqual(wire.dependsOn);
  });

  it("leaves an unconditional task without a conditions map", () => {
    expect(fromWireTask(task(["a", "b"])).conditions).toBeUndefined();
  });

  it("drops the canvas position the backend never stored", () => {
    // The API rejects unknown task fields now, so a leftover `position` in
    // the payload would 422 every save from the designer.
    const model: ConduitTask = { ...task(["a"]), position: { x: 12, y: 34 } };
    const wire = toWireTask(model);

    expect("position" in wire).toBe(false);
    expect(model.position).toEqual({ x: 12, y: 34 });
  });

  it("collapses a source task listed twice instead of duplicating it", () => {
    // `conditions` is keyed by source task, so both entries cannot survive.
    // Keeping the first is what stops the round trip from emitting the
    // conditional form twice.
    const model = fromWireTask(task(["a", "a.output.match(X)"]));

    expect(model.dependsOn).toEqual(["a"]);
    expect(model.conditions).toBeUndefined();
    expect(toWireTask(model).dependsOn).toEqual(["a"]);
  });
});

describe("withoutCondition", () => {
  it("drops the gate for the removed dependency", () => {
    const t = task(["a", "b"], {
      a: { kind: "match", pattern: "X" },
      b: { kind: "match", pattern: "Y" },
    });
    expect(withoutCondition(t, "a")).toEqual({ b: { kind: "match", pattern: "Y" } });
  });

  it("collapses to undefined once the last condition goes", () => {
    const t = task(["a"], { a: { kind: "match", pattern: "X" } });
    expect(withoutCondition(t, "a")).toBeUndefined();
  });

  it("is a no-op for a dependency that had no condition", () => {
    const t = task(["a", "b"], { a: { kind: "match", pattern: "X" } });
    expect(withoutCondition(t, "b")).toEqual(t.conditions);
  });
});

describe("renameConditionSource", () => {
  it("re-keys the gate so a rename does not strand it", () => {
    const conditions = { build: { kind: "match" as const, pattern: "ok" } };
    expect(renameConditionSource(conditions, "build", "compile")).toEqual({
      compile: { kind: "match", pattern: "ok" },
    });
  });

  it("leaves unrelated conditions alone", () => {
    const conditions = { build: { kind: "match" as const, pattern: "ok" } };
    expect(renameConditionSource(conditions, "other", "x")).toBe(conditions);
  });
});

describe("stripSurroundingQuotes", () => {
  it.each([
    ['"PASS"', "PASS"],
    ["'PASS'", "PASS"],
    ["PASS", "PASS"],
    ['"PASS', '"PASS'],
    ['""', '""'],
    ['\\"PASS\\"', '\\"PASS\\"'],
  ])("%s -> %s", (raw, expected) => {
    expect(stripSurroundingQuotes(raw)).toBe(expected);
  });
});
