import { CONDUIT_INPUTS_STORAGE_KEY, CONDUIT_STORAGE_TTL_MS } from "@/constants/dashboard";

// Conduit inputs are free-form and may hold secrets (tokens, passwords). Give
// saved values a lifespan instead of keeping them in localStorage forever, so a
// shared machine doesn't leak them indefinitely. Legacy entries without a
// `savedAt` are treated as expired and silently dropped on read.
interface StoredEntry {
  inputs: Record<string, string>;
  savedAt: number;
}

function loadAll(): Record<string, StoredEntry> {
  try {
    const raw = localStorage.getItem(CONDUIT_INPUTS_STORAGE_KEY);
    if (!raw) return {};
    const data: Record<string, StoredEntry> = JSON.parse(raw);
    if (!data || typeof data !== "object" || Array.isArray(data)) return {};
    const oldest = Date.now() - CONDUIT_STORAGE_TTL_MS;
    const entries = Object.entries(data);
    const fresh = entries.filter(([, entry]) => Number.isFinite(entry?.savedAt) && entry.savedAt >= oldest);
    if (fresh.length === entries.length) return data;
    const remaining = Object.fromEntries(fresh);
    if (fresh.length === 0) {
      localStorage.removeItem(CONDUIT_INPUTS_STORAGE_KEY);
    } else {
      saveAll(remaining);
    }
    return remaining;
  } catch {
    return {};
  }
}

function saveAll(data: Record<string, StoredEntry>): void {
  localStorage.setItem(CONDUIT_INPUTS_STORAGE_KEY, JSON.stringify(data));
}

export function loadConduitInputs(
  conduitName: string,
): Record<string, string> | null {
  return loadAll()[conduitName]?.inputs ?? null;
}

export function saveConduitInputs(
  conduitName: string,
  inputs: Record<string, string>,
): void {
  saveAll({ ...loadAll(), [conduitName]: { inputs, savedAt: Date.now() } });
}
