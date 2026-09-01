// The API client.  The path is always the relative '/api' —— vite's proxy forwards it in
// development and nginx in production.  Which is why there is no per-environment base URL branch.

import { tr } from './i18n/lang';
import S from './i18n/strings/api';

export type Step = {
  id: string; title: string; desc: string;
  /** main = the ordered main pipeline · combo = one that bundles those · partial · check */
  group: 'main' | 'combo' | 'partial' | 'check';
  /** The order within main.  0 for the rest */
  order: number;
  /** The step ids a combo re-runs */
  runs?: string[];
  reads?: string;
  writes?: string;
  cmd: string[]; minutes: number;
  /** A step needing `claude -p`.  A container may have no authentication. */
  needs_llm: boolean;
  writes_db: boolean;
  /** It deletes and rewrites documents inside the user's vault — a confirmation is mandatory */
  writes_vault?: boolean;
};

/** A **live** estimate for one step —— unlike the fixed `minutes`, it counts the work left right now.
 *
 * Where `units` exists it is the more trustworthy: "3 LLM calls" is checkable in a way
 * "about 30 minutes" is not.  A step without it (promote, export and so on) uses the wall clock from the run history.
 */
export type Estimate = {
  step: string; title: string;
  /** The estimated seconds.  A fixed start-up cost + the work left × the per-unit rate */
  seconds: number;
  /** The count of work left.  null for a step that cannot be counted */
  units: number | null;
  /** The unit name for units ("LLM calls" · "chunks" …) */
  unit: string;
  /** One line on where that number came from */
  basis: string;
  /** Where the rate came from —— "the run history's median" or "a seed value (too little history)" */
  source: string;
  declared_minutes?: number;
  parts?: Estimate[];
};

export type Group = { id: string; title: string; desc: string };

/** Is the LLM usable.  If not, that step's button is blocked. */
export type LlmStatus = { ok: boolean; message: string };

export type Run = {
  id: string; step: string; args?: string[];
  started_at: number; ended_at?: number; exit_code?: number;
  status: 'running' | 'ok' | 'failed' | 'cancelled'; lines: number;
  /** Where it was run —— the web UI when absent */
  origin?: 'cli';
};

export type Recommendation = { step: string; why: string; severity: 'high' | 'medium' | 'low' };

export type Status = {
  db: {
    path: string; tables: Record<string, number>; disk_bytes: number;
    built_at: number; embedding_model: string; schema_version: string;
    /** The vault used to **build** the DB */
    vault_path: string;
    /** The vault being **read** now (in a container, the mount source's host path) */
    vault_now: string;
  };
  documents: {
    indexed: number; origin: Record<string, number>;
    added: string[]; modified: string[]; deleted: string[];
  };
  kg: {
    entities: number; relations: number;
    stale: { path: string; reason: string; marked_at: number }[];
    stale_ratio: number; warn_ratio: number;
    extracted_at: number; extract_drift: string[];
  };
  artifacts: { mtimes: Record<string, number>; stale: string[] };
  checked_at: number;
  recommend: Recommendation[];
};

export type Suggestion = {
  type: string; reason: string;
  a: string; a_docs: number; b: string; b_docs: number;
  already_aliased: boolean;
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!r.ok) {
    // The server gives {error: "..."}.  Showing that as-is is more useful than a status code.
    const body = await r.json().catch(() => null);
    throw new Error(body?.error ?? `${r.status} ${r.statusText}`);
  }
  return r.json() as Promise<T>;
}


/** The settings —— the priority is owned by the server (`src/kal_config.py`).  env > config.json > the defaults. */
export type ConfigPath = {
  key: string; label: string; value: string;
  source: 'env' | 'default';
  env: string; default: string;
  exists: boolean;
  /** The path is a bind mount in a container, so it **cannot be changed at runtime.**  Always false. */
  editable: false;
  how: string;
};
export type ConfigSetting = {
  key: string; value: string | number;
  source: 'env' | 'file' | 'default';
  default: string | number;
  type: 'int' | 'float' | 'str';
  env: string;
  /** true means a **re-index** is needed after changing it for the DB to reflect it. */
  reindex: boolean;
  min: number | null; max: number | null;
  label: string; help: string;
};
export type ConfigView = {
  config_path: string;
  in_container: boolean;
  paths: ConfigPath[];
  settings: ConfigSetting[];
  /** The difference between the current settings and **the values the DB was built with**.  It should be empty. */
  drift: { key: string; now: string; db: string }[];
  error: string;
};

export const api = {
  llm: (fresh = false) => req<LlmStatus>(`/llm${fresh ? '?fresh=1' : ''}`),
  status: (fresh = false) => req<Status>(`/status${fresh ? '?fresh=1' : ''}`),
  steps: () => req<{ steps: Step[]; groups: Group[] }>('/steps'),
  //  The screen must not die on a failure —— the server gives {} and the screen falls back to fixed values.
  estimate: () => req<Record<string, Estimate>>('/estimate'),
  runs: () => req<Run[]>('/runs'),
  run: (id: string) => req<Run>(`/runs/${id}`),
  start: (step: string, args: string[] = []) =>
    req<Run>('/runs', { method: 'POST', body: JSON.stringify({ step, args }) }),
  cancel: (id: string) => req<{ status: string }>(`/runs/${id}/cancel`, { method: 'POST' }),
  config: () => req<ConfigView>('/config'),
  saveConfig: (patch: Record<string, string | number>) =>
    req<ConfigView>('/config', { method: 'PUT', body: JSON.stringify(patch) }),
  aliases: () => req<{ content: string }>('/aliases'),
  saveAliases: (content: string) =>
    req<{ saved: boolean; note: string }>('/aliases', {
      method: 'PUT', body: JSON.stringify({ content }),
    }),
  suggestions: (weak = false) =>
    req<Suggestion[]>(`/aliases/suggestions${weak ? '?weak=1' : ''}`),

  // Homonyms —— the opposite direction to aliases (one name → several nodes).
  // Why the candidates are **YAML to paste** rather than JSON: a person has to fill in the cues,
  // so structuring it only ends up in an editor anyway.
  homonyms: () => req<{ content: string }>('/homonyms'),
  saveHomonyms: (content: string) =>
    req<{ saved: boolean; note: string }>('/homonyms', {
      method: 'PUT', body: JSON.stringify({ content }),
    }),
  homonymSuggestions: (weak = false) =>
    req<{ yaml: string }>(`/homonyms/suggestions${weak ? '?weak=1' : ''}`),
};

/** The knowledge graph the galaxy view draws.
 *
 * Why req<T> is not used —— the vault name arrives as **a header**.  It is not in the graph JSON,
 * and the viewer needs it to build obsidian:// deep links.
 *
 * At around 6MB the cache matters.  The server sends Last-Modified, so a second visit ends in a
 * 304, and re-running the export changes the mtime and it is fetched again automatically.
 */
export async function fetchGraph(): Promise<{ graph: unknown; vaultName: string }> {
  const r = await fetch('/api/graph', { headers: { Accept: 'application/json' } });
  if (!r.ok) {
    const msg = await r.json().catch(() => null);
    throw new Error((msg as { error?: string } | null)?.error ?? tr(S)('graphReadFailed')(r.status));
  }
  return { graph: await r.json(), vaultName: r.headers.get('X-Vault-Name') ?? 'vault' };
}

/** Connect the run log over SSE.  onDone fires when it finishes.  Calling the return value disconnects. */
export function streamLog(
  id: string,
  onLine: (line: string) => void,
  onDone: (status: string) => void,
): () => void {
  const es = new EventSource(`/api/runs/${id}/log`);
  es.onmessage = (e) => onLine(e.data);
  es.addEventListener('done', (e) => { onDone((e as MessageEvent).data); es.close(); });
  // When the server finishes, EventSource tries to reconnect.  If done has already arrived it is
  // closed; otherwise it really was cut and reconnecting is right —— the default behaviour is left alone.
  return () => es.close();
}
