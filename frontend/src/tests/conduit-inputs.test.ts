// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadConduitInputs, saveConduitInputs } from "@/services/api/conduit-inputs";
import { CONDUIT_INPUTS_STORAGE_KEY } from "@/constants/dashboard";

describe("conduit-inputs persistence", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.useRealTimers());

  it("round-trips saved inputs", () => {
    saveConduitInputs("deploy", { token: "abc" });
    expect(loadConduitInputs("deploy")).toEqual({ token: "abc" });
  });

  it("returns null for an unknown conduit", () => {
    expect(loadConduitInputs("missing")).toBeNull();
  });

  it("drops values older than the TTL", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    saveConduitInputs("deploy", { token: "abc" });
    // 8 days later — past the 7-day lifespan.
    vi.setSystemTime(8 * 24 * 60 * 60 * 1000);
    expect(loadConduitInputs("deploy")).toBeNull();
    expect(localStorage.getItem(CONDUIT_INPUTS_STORAGE_KEY)).toBeNull();
  });

  it("treats legacy entries without a timestamp as expired", () => {
    localStorage.setItem(
      CONDUIT_INPUTS_STORAGE_KEY,
      JSON.stringify({ deploy: { token: "abc" } }),
    );
    expect(loadConduitInputs("deploy")).toBeNull();
    expect(localStorage.getItem(CONDUIT_INPUTS_STORAGE_KEY)).toBeNull();
  });

  it("prunes all expired entries during a read and preserves fresh entries", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    saveConduitInputs("deploy", { token: "expired-token" });
    vi.setSystemTime(2 * 24 * 60 * 60 * 1000);
    saveConduitInputs("build", { branch: "main" });
    vi.setSystemTime(8 * 24 * 60 * 60 * 1000);

    expect(loadConduitInputs("build")).toEqual({ branch: "main" });
    const stored = JSON.parse(localStorage.getItem(CONDUIT_INPUTS_STORAGE_KEY)!);
    expect(Object.keys(stored)).toEqual(["build"]);
  });
});
