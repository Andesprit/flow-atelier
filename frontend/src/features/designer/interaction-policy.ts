import type { InteractionPolicy } from "@/types/conduit";

export function needsSupervisor(policy?: InteractionPolicy | null): boolean {
  return policy?.questions === "supervisor" || policy?.questions === "hybrid"
    || policy?.permissions === "supervisor" || policy?.permissions === "hybrid";
}

/** Mirrors the backend's harness-name grammar without restricting custom harnesses. */
export function interactionError(policy?: InteractionPolicy | null): string | null {
  if (!needsSupervisor(policy)) return null;
  return /^harness:[a-z0-9][a-z0-9-]*$/.test(policy?.supervisor?.tool ?? "")
    ? null
    : "Enter a harness name such as harness:codex.";
}
