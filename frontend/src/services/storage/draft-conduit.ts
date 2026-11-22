import { CONDUIT_STORAGE_TTL_MS, DRAFT_CONDUIT_STORAGE_KEY } from "@/constants/dashboard";
import type { Conduit } from "@/types/conduit";

export function loadDraftConduit(): Conduit | null {
  try {
    const raw = localStorage.getItem(DRAFT_CONDUIT_STORAGE_KEY);
    if (!raw) return null;
    const parsed: Conduit & { savedAt?: number } = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || Array.isArray(parsed.inputs)) {
      clearDraftConduit();
      return null;
    }
    const { savedAt, ...conduit } = parsed;
    if (savedAt === undefined) {
      // Preserve drafts written before timestamps were added.
      saveDraftConduit(conduit);
    } else if (!Number.isFinite(savedAt) || Date.now() - savedAt > CONDUIT_STORAGE_TTL_MS) {
      clearDraftConduit();
      return null;
    }
    return conduit;
  } catch {
    return null;
  }
}

export function saveDraftConduit(conduit: Conduit): void {
  localStorage.setItem(DRAFT_CONDUIT_STORAGE_KEY, JSON.stringify({ ...conduit, savedAt: Date.now() }));
}

export function clearDraftConduit(): void {
  localStorage.removeItem(DRAFT_CONDUIT_STORAGE_KEY);
}
