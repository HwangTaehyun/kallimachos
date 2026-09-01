import type { App } from 'obsidian';
import { normalizePath } from 'obsidian';
import { matchesFilter, type FilterQuery } from './noteFilter';
import type { GraphData, GraphNode, GraphLink } from '../types';

/**
 * The Knowledge DB data source.
 *
 * The default Galaxy View draws the vault's note-link graph (resolvedLinks) —— a node is a
 * "page".  This module puts **an entity and relation graph** in its place.  The nodes are
 * concepts, tools and people, and the edges are relations an LLM extracted.
 *
 * The data is read from JSON exported out of LanceDB (export_kdb_graph.py).  Creating kg/ notes
 * inside the vault for Obsidian to read as [[links]] is another option, but that swells the
 * vault by hundreds of pages and buries the curated notes.  JSON inside the plugin folder shows
 * up in neither note search nor the default graph.
 */

export const KDB_PREFIX = 'kdb:';
//  ⚠ The rename (kdb→kal) had not reached this subtree.  The pipeline uses `kal-graph.json`
//     (src/export_kal_graph.py:164 · api/main.go) while this was reading the old name, and with
//     the file absent `loadKdbGraph` quietly returns null, so the entity graph comes up
//     **empty with no error.**  (2026-08-25 deep review)
//     No fallback to the old name is kept —— maintaining two paths for something one
//     `just run export` settles turns it into permanent debt.
export const KDB_JSON_PATH = 'plugins/kal-galaxy/kal-graph.json';

export interface KdbTypeInfo {
	name: string;
	count: number;
	color: string;
}

/** What filtering and colour group by.  Type = 'what it is', community = 'what it is used with' */
export type KdbGroupBy = 'type' | 'community';

/** A Louvain community.  The name joins its top 3 entities by degree (export_kdb_graph.py) */
export interface KdbCommunity {
	id: number;
	count: number;
	/** The topic name the LLM wrote (export_kdb_graph.label_communities) */
	name: string;
	/** 3 representative entities.  They show 'what is actually in it', which the name alone cannot */
	members?: string;
	/** A one-sentence summary.  Shown at the top on hover */
	summary?: string;
	color: string;
}

/** Entity detail for the card.  The fields that do not fit in GraphNode. */
export interface KdbEntityMeta {
	entityId: number;
	type: string;
	desc: string;
	docs: string[]; // vault-relative paths
}

/** Relation detail for the card.  One edge joining two nodes. */
export interface KdbRelationMeta {
	source: number; // the node index
	target: number;
	desc: string;
	kw: string[];
	docs: string[];
}

export interface KdbGraph {
	data: GraphData;
	/** The same index as nodes */
	meta: KdbEntityMeta[];
	/** The same index as links */
	linkMeta: KdbRelationMeta[];
	types: KdbTypeInfo[];
	/**
	 * Per-type counts for the legend.  Measured **before the type filter is applied**.
	 * Counted on the filtered result, turning a type off would remove it from the list and it could never be turned back on.
	 * (The degree floor and the name query are reflected — those really are "the current graph's population".)
	 */
	typeCounts: { folder: string; count: number }[];
	/** Group name → its assigned colour (hex).  Stops 21 communities cycling through the wheel's 9 slots. */
	groupColors: Map<string, string>;
	/** Group name → its description (summary, representative entities).  Used by the legend hover.  Empty in type mode */
	groupInfo: Map<string, { summary: string; members: string }>;
	/** Node index → adjacent node indices (undirected) */
	adjacency: number[][];
	builtAt: string;
	minDegree: number;
}

interface RawEntity {
	id: number;
	/** The Louvain community id.  -1 = other (a community too small to put in the filter) */
	comm?: number;
	name: string;
	type: string;
	deg: number;
	desc: string;
	docs: string[];
}
interface RawRelation {
	s: number;
	t: number;
	desc: string;
	kw: string[];
	docs: string[];
}
interface RawFile {
	version?: number;
	built_at?: string;
	min_degree?: number;
	types?: KdbTypeInfo[];
	communities?: KdbCommunity[];
	entities?: RawEntity[];
	relations?: RawRelation[];
}

export async function kdbJsonExists(app: App): Promise<boolean> {
	const p = normalizePath(`${app.vault.configDir}/${KDB_JSON_PATH}`);
	return app.vault.adapter.exists(p);
}

/**
 * Reads the JSON and converts it to GraphData.
 *
 * Only one part of the mapping needs explaining: `folderTop` is given the entity's **type or community**.
 *
 * That field drives the colour groups and the legend, so leaving it there gives colour-by-type
 * for free.  Reusing a folder field for data with no concept of folders makes the name wrong,
 * but that is cheap for what it buys without touching the renderer or the overlay.
 */
export async function loadKdbGraph(
	app: App,
	opts: {
		nodeCap: number | null;
		linkCap: number | null;
		minDegree?: number;
		/** Group by type or by community.  The FILTER list and the colour groups change together. */
		groupBy?: KdbGroupBy;
		/** The groups to switch off.  Unchecking in the FILTER list arrives here (a type name or a community name). */
		hiddenTypes?: ReadonlySet<string>;
		/** The 'Filter by name' query.  Entities have no path, so it applies to the name alone. */
		query?: FilterQuery;
	},
): Promise<KdbGraph | null> {
	const p = normalizePath(`${app.vault.configDir}/${KDB_JSON_PATH}`);
	if (!(await app.vault.adapter.exists(p))) return null;

	let raw: RawFile;
	try {
		raw = JSON.parse(await app.vault.adapter.read(p)) as RawFile;
	} catch {
		return null;
	}
	const ents = raw.entities ?? [];
	const rels = raw.relations ?? [];
	if (ents.length === 0) return null;

	// Cut from the highest degree down.  Over the cap, keeping the hubs is what leaves a readable picture.
	const nodeCap = opts.nodeCap ?? Infinity; // null = no cap
	const linkCap = opts.linkCap ?? Infinity;
	const minDegree = Math.max(1, opts.minDegree ?? 1);
	const degOf = (i: number): number => ents[i]?.deg ?? 0;
	// The type and name filters are applied **here**.  The note graph filters ahead of buildGraph,
	// but KDB's links reference node **indices**, so removing nodes later puts meta, linkMeta and
	// adjacency all out of step.  This point, where the set to keep is decided, is the only safe one.
	const groupBy = opts.groupBy ?? 'type';
	const commById = new Map((raw.communities ?? []).map((c) => [c.id, c]));
	const OTHER = 'Other';
	/** The name of the group this entity belongs to.  Filtering, colour and the legend all read this value. */
	const groupOf = (e: RawEntity): string =>
		groupBy === 'community' ? (commById.get(e.comm ?? -1)?.name ?? OTHER) : e.type;
	const hidden = opts.hiddenTypes;
	const q = opts.query ?? [];
	const nameOk = (name: string): boolean =>
		q.length === 0 || matchesFilter({ path: name, basename: name }, q);
	// The degree floor —— most are concepts brushed past once or twice, and filtering them is what makes the structure readable.
	// It could be cut at the export step too, but that needs a rebuild.  Here it takes effect at once.
	let keepIdx: number[] = ents
		.map((_, i) => i)
		.filter((i) => {
			const e = ents[i];
			if (!e || degOf(i) < minDegree) return false;
			if (hidden?.has(groupOf(e))) return false;
			return nameOk(e.name);
		});
	// The verdict has to use the post-filter length.  Reading ents.length, a degree filter leaving
	// only 100 would still sort and slice because the original exceeded the cap, and conversely a
	// case that still exceeds the cap after filtering could be missed.
	if (keepIdx.length > nodeCap) {
		keepIdx = keepIdx.sort((a, b) => degOf(b) - degOf(a)).slice(0, nodeCap);
		keepIdx.sort((a, b) => a - b); // restore the original order — it keeps the link remapping simple
	}
	// The legend counts without the type filter (see the typeCounts comment above)
	// The colours come from the data (types: TYPE_COLOR · communities: COMM_COLORS)
	const groupColors = new Map<string, string>();
	const groupInfo = new Map<string, { summary: string; members: string }>();
	if (groupBy === 'community') {
		for (const c of raw.communities ?? []) {
			groupColors.set(c.name, c.color);
			groupInfo.set(c.name, { summary: c.summary ?? '', members: c.members ?? '' });
		}
	} else {
		for (const t of raw.types ?? []) groupColors.set(t.name, t.color);
	}

	const legendCnt = new Map<string, number>();
	for (let i = 0; i < ents.length; i++) {
		const e = ents[i];
		if (!e || degOf(i) < minDegree || !nameOk(e.name)) continue;
		legendCnt.set(groupOf(e), (legendCnt.get(groupOf(e)) ?? 0) + 1);
	}
	const typeCounts = [...legendCnt]
		.map(([folder, count]) => ({ folder, count }))
		// 'other' is not a community but 'everything left uncommunitied', so it goes last regardless of size
		.sort((a, b) => (a.folder === OTHER ? 1 : b.folder === OTHER ? -1 : b.count - a.count));

	const remap = new Map<number, number>();
	keepIdx.forEach((old, next) => remap.set(old, next));

	const nodes: GraphNode[] = [];
	const meta: KdbEntityMeta[] = [];
	for (const old of keepIdx) {
		const e = ents[old];
		if (!e) continue;
		nodes.push({
			id: `${KDB_PREFIX}${e.id}`,
			name: e.name,
			folderTop: groupOf(e), // ← the colour groups and the legend read this field (a type or a community)
			degree: 0, // recounted below from the real links
			inDegree: 0,
			outDegree: 0,
			fileSize: 0,
			tags: [`#${e.type}`],
			unresolved: false,
			tag: false,
		});
		meta.push({ entityId: e.id, type: e.type, desc: e.desc, docs: e.docs ?? [] });
	}

	const links: GraphLink[] = [];
	const linkMeta: KdbRelationMeta[] = [];
	const adjacency: number[][] = nodes.map(() => []);
	for (const r of rels) {
		const s = remap.get(r.s);
		const t = remap.get(r.t);
		if (s === undefined || t === undefined || s === t) continue;
		if (links.length >= linkCap) break;
		const ns = nodes[s];
		const nt = nodes[t];
		if (!ns || !nt) continue;
		links.push({ source: s, target: t });
		linkMeta.push({ source: s, target: t, desc: r.desc, kw: r.kw ?? [], docs: r.docs ?? [] });
		adjacency[s]?.push(t);
		adjacency[t]?.push(s);
		// Undirected, so there is no in/out.  Both sides are incremented so the degree total adds up.
		ns.degree++;
		nt.degree++;
		ns.outDegree++;
		nt.inDegree++;
	}

	// ⚠ Do not filter on folderTop —— in community mode it holds a community name, so comparing it
	//   against an entity type gives **an always-empty array**.  The type list comes from the
	//   original regardless of the grouping method.  (2026-08-18 review)
	const types = raw.types ?? [];

	return {
		data: { nodes, links },
		meta,
		linkMeta,
		types,
		typeCounts,
		groupColors,
		groupInfo,
		adjacency,
		builtAt: raw.built_at ?? '',
		minDegree: raw.min_degree ?? 1,
	};
}

/**
 * The depth filter —— keeps only the nodes within N hops of the starting node.
 *
 * Why it is needed: the entity graph has thousands of nodes, and showing all of them makes
 * individual relations unreadable.  Narrowing to something like "2 hops around this concept" is
 * what makes it actually explorable.
 *
 * It returns the set of node indices to keep.  The caller uses it as a render filter.
 */
export function nodesWithinDepth(adjacency: number[][], roots: number[], depth: number): Set<number> {
	const keep = new Set<number>(roots);
	if (depth <= 0) return keep;
	let frontier = roots.slice();
	for (let d = 0; d < depth; d++) {
		const next: number[] = [];
		for (const u of frontier) {
			for (const v of adjacency[u] ?? []) {
				if (!keep.has(v)) {
					keep.add(v);
					next.push(v);
				}
			}
		}
		if (next.length === 0) break;
		frontier = next;
	}
	return keep;
}
