import type { App } from 'obsidian';
import { normalizePath } from 'obsidian';

export interface GhostEdgeRecord {
	source: string; // a vault-relative path (= GraphNode.id)
	target: string;
	state: 'pending' | 'deferred';
	score: number; // 0..1
}

/**
 * Reads the ghost-edge protocol file the Constellation companion plugin writes (read-only, never written back).
 * Protocol version 1; a higher version is ignored wholesale (fail-safe).  A missing or corrupted file always returns null ——
 * the companion visualisation is an enhancement and must never break the main feature.  The posture matches graphJsonImport.ts exactly.
 */
export async function readGhostEdges(app: App): Promise<GhostEdgeRecord[] | null> {
	try {
		const path = normalizePath(app.vault.configDir + '/plugins/constellation/ghost-edges.json');
		if (!(await app.vault.adapter.exists(path))) return null;
		const parsed = JSON.parse(await app.vault.adapter.read(path)) as {
			version?: unknown;
			edges?: unknown[];
		};
		if (typeof parsed.version !== 'number' || parsed.version > 1) return null; // the version gate
		const out: GhostEdgeRecord[] = [];
		for (const e of parsed.edges ?? []) {
			const r = e as { source?: unknown; target?: unknown; state?: unknown; score?: unknown };
			if (typeof r.source !== 'string' || typeof r.target !== 'string' || r.source === r.target) continue;
			out.push({
				source: r.source,
				target: r.target,
				state: r.state === 'deferred' ? 'deferred' : 'pending',
				score: typeof r.score === 'number' && isFinite(r.score) ? Math.min(Math.max(r.score, 0), 1) : 0.5,
			});
		}
		return out;
	} catch {
		return null;
	}
}
