import type { App } from 'obsidian';
import { normalizePath } from 'obsidian';

export interface ColorGroup {
	query: string; // already trimmed (the real config carries trailing spaces)
	color: string; // #rrggbb
}

/**
 * Reads the built-in graph's colorGroups (.obsidian/graph.json, an undocumented format —— parsed best-effort, returning null on failure).
 * The colour is stored as a decimal int; a query looks like "path:01study  " (note the trailing spaces).  Read-only, never written back.
 */
export async function readGraphColorGroups(app: App): Promise<ColorGroup[] | null> {
	try {
		const path = normalizePath(app.vault.configDir + '/graph.json');
		if (!(await app.vault.adapter.exists(path))) return null;
		const parsed = JSON.parse(await app.vault.adapter.read(path)) as {
			colorGroups?: { query?: unknown; color?: { rgb?: unknown } }[];
		};
		const groups: ColorGroup[] = [];
		for (const g of parsed.colorGroups ?? []) {
			const query = typeof g.query === 'string' ? g.query.trim() : '';
			const rgb = g.color?.rgb;
			if (!query || typeof rgb !== 'number') continue;
			groups.push({ query, color: `#${(rgb & 0xffffff).toString(16).padStart(6, '0')}` });
		}
		return groups;
	} catch {
		return null;
	}
}
