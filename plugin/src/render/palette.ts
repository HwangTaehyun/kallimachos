import { Color } from 'three';
import { hash32 } from '../data/seed';
import type { GraphNode } from '../types';
import type { ColorGroup } from '../settings/graphJsonImport';
import { primaryTag } from '../data/tagLens';

export type NodeColorFn = (node: GraphNode) => Color;

/**
 * The user's 2D graph palette → the node colouring function.
 * Semantically aligned with the built-in graph: a path: prefix match, the first hit top-to-bottom winning; no hit falls back to the hash palette.
 */
export function makeNodeColorFn(groups: ColorGroup[]): NodeColorFn {
	const parsed = groups.map((g) => ({
		prefix: g.query.startsWith('path:') ? g.query.slice(5).trim() : null,
		raw: g.query,
		color: new Color(g.color),
	}));
	return (node) => {
		if (node.tag) return TAG;
		if (node.unresolved) return UNRESOLVED;
		for (const g of parsed) {
			if (g.prefix !== null ? node.id.startsWith(g.prefix) : node.id.includes(g.raw)) return g.color;
		}
		return folderColor(node.folderTop, false);
	};
}

export const fallbackColorFn: NodeColorFn = (node) =>
	node.tag ? TAG : folderColor(node.folderTop, node.unresolved);

const tagColorCache = new Map<string, Color>();

/**
 * Tag colours cover only notes with a primary tag and the hubs; an untagged or unresolved node falls back to the existing folder or imported palette.
 * The hash maps straight onto 360 hues, so a new tag does not change an existing tag's colour through a rank shift.
 */
export function makeTagColorFn(base: NodeColorFn): NodeColorFn {
	return (node) => {
		if (node.unresolved) return base(node);
		const tag = primaryTag(node);
		if (!tag) return base(node);
		let color = tagColorCache.get(tag);
		if (!color) {
			color = new Color().setHSL((hash32(tag) % 360) / 360, 0.68, 0.6);
			tagColorCache.set(tag, color);
		}
		return color;
	};
}

// The hue wheel of Obsidian's standard colour family hsl(h, 60%, 60%) (the same family as Rick's 9 colour groups);
// with colorGroups present that set wins, and this table is the fallback for unconfigured folders
const HUES = [0, 40, 80, 120, 160, 200, 240, 280, 320];

const NEUTRAL = new Color('#9aa4b2'); // ungrouped
const UNRESOLVED = new Color('#7a8499'); // a ghost
const TAG = new Color('#d8a94b'); // a tag node: warm amber, distinct from the folder hues and the unresolved grey, reading as "meta/structure"

const cache = new Map<string, Color>();

/**
 * Group name → its assigned colour.  Used when the data brings the colours itself (KDB's types and communities).
 *
 * Why it is needed: assignFolderHues cycles through HUES's 9 slots.  That collision was already
 * met with 14 folders (the comment above), and entity **communities number 21**, so it overlaps
 * threefold — communities 0, 9 and 18 end up the same colour and cannot be told apart in the galaxy.
 * export_kdb_graph.py computes a unique colour per community and sends it, so that is used as-is.
 */
const explicit = new Map<string, Color>();

export function setExplicitColors(colors: ReadonlyMap<string, string>): void {
	explicit.clear();
	for (const [name, hex] of colors) {
		try {
			explicit.set(name, new Color(hex));
		} catch {
			/* An invalid colour string is ignored and it falls through to the hue-wheel rotation */
		}
	}
	cache.clear();          // a previous assignment left behind would stop the new colour taking
}

/**
 * Hand out the fallback hues ranked by note count, replacing the original `HUES[hash32(folder) % 9]`.
 *
 * The original's defect (measured on Rick's library): 14 top-level folders collide over 9 hue slots, and the hash is unordered →
 * 99Archive (545 notes) / 90Oldpapers (86) / Readwise (68) all took **the same blue**, 1,184 notes = 37% of the library
 * landing on colours that cannot be told apart.  Once 0.5.0 put the colour legend in the panel, that defect would be placed directly in front of the user.
 *
 * The fix has two parts: ① hand out hues in descending note count, so the large folders get the non-colliding ones first; ② **hand them only to folders that really need the fallback** ——
 * one already covered by colorGroups takes no slot (Rick's 9 imported groups cover 9 folders, leaving 5 to take one distinct hue each).
 * With more than 9 folders awaiting a hue it still recycles them (the wheel is only so big), but what collides is the smallest few, no longer the largest.
 *
 * @param foldersByCount the top-level folders, **descending by note count** (see noteFilter.folderStats)
 * @param covered whether that folder is already covered by colorGroups (a covered one takes no hue slot)
 */
export function assignFolderHues(foldersByCount: readonly string[], covered: (folder: string) => boolean): void {
	// What has an explicit colour is not cleared from the cache
	for (const k of [...cache.keys()]) if (!explicit.has(k)) cache.delete(k);
	let i = 0;
	for (const f of foldersByCount) {
		if (f === '' || covered(f) || explicit.has(f)) continue;
		const hue = HUES[i % HUES.length] ?? 0;
		cache.set(f, new Color().setHSL(hue / 360, 0.6, 0.6));
		i++;
	}
}

/** Decide whether a top-level folder has already been taken by a colour group (semantically aligned with makeNodeColorFn's matching rule) */
export function folderCoveredByGroups(folder: string, groups: readonly ColorGroup[]): boolean {
	return groups.some((g) => {
		const q = g.query.startsWith('path:') ? g.query.slice(5).trim() : null;
		if (q === null) return folder.includes(g.query);
		// A group prefix matches node.id (the full path); a folder is covered ⟺ every path under it starts with that prefix
		return `${folder}/`.startsWith(q) || folder === q.replace(/\/$/, '');
	});
}

export function folderColor(folderTop: string, unresolved: boolean): Color {
	if (unresolved) return UNRESOLVED;
	if (folderTop === '') return NEUTRAL;
	const ex = explicit.get(folderTop);
	if (ex) return ex;
	let c = cache.get(folderTop);
	if (!c) {
		// A folder that appeared after assignFolderHues (created between rebuilds): fall back to the hash, and it takes its place at the next rebuild
		const hue = HUES[hash32(folderTop) % HUES.length] ?? 0;
		c = new Color().setHSL(hue / 360, 0.6, 0.6);
		cache.set(folderTop, c);
	}
	return c;
}

/** The link colour: a 50/50 blend of the endpoint colours → desaturated 60% + dimmed (NASA's thin grey lines, with the glow coming from bloom) */
export function linkColor(a: Color, b: Color): Color {
	const c = a.clone().lerp(b, 0.5);
	const hsl = { h: 0, s: 0, l: 0 };
	c.getHSL(hsl);
	c.setHSL(hsl.h, hsl.s * 0.4, Math.min(hsl.l, 0.35));
	return c;
}
