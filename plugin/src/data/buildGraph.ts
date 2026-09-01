import type { GraphData, GraphLink, GraphNode } from '../types';
import { boundedTagHubLimit, topTags } from './tagLens';


/** A plain record for input, not depending on obsidian —— unit-testable (a design requirement) */
export interface FileRecord {
	path: string;
	basename: string;
	size?: number; // bytes
	tags?: string[]; // the getAllTags result (with the # prefix, frontmatter + body); used only with includeTags
}

export type LinkTable = Record<string, Record<string, number>>;

export interface BuildOptions {
	includeUnresolved: boolean;
	includeOrphans: boolean;
	/** Read the note tags and pass them through; it generates no nodes or edges of its own. */
	includeTags?: boolean;
	/** Top-N tag hubs are generated only with includeTags also on. */
	includeTagHubs?: boolean;
	/** The user setting's range is 5–50; the build side closes it again, so an old or corrupted save cannot make an unbounded graph. */
	tagHubLimit?: number;
	/** The mobile tier: take the top N nodes by degree (keeping the hub structure), null = unlimited */
	nodeCap?: number | null;
	/** The mobile tier: take the top N edges by min(endpoint degree), null = unlimited */
	linkCap?: number | null;
}

/** The top-level folder = the colour grouping key, and the granularity of the "filter" legend; a note at the root returns '' */
export function topFolder(path: string): string {
	const idx = path.indexOf('/');
	return idx === -1 ? '' : path.slice(0, idx);
}

/**
 * A vault snapshot → the graph model.
 * - Only targets inside the files set are taken (attachments and other non-graph targets are dropped)
 * - resolvedLinks is already deduplicated by (src,dst) (the value is the occurrence count), so no further deduplication is needed
 * - degree = out-degree + in-degree
 */
export function buildGraph(
	files: FileRecord[],
	resolvedLinks: LinkTable,
	unresolvedLinks: LinkTable,
	opts: BuildOptions,
): GraphData {
	const nodes: GraphNode[] = [];
	const indexById = new Map<string, number>();

	for (const f of files) {
		indexById.set(f.path, nodes.length);
		nodes.push({
			id: f.path,
			name: f.basename,
			folderTop: topFolder(f.path),
			degree: 0,
			inDegree: 0,
			outDegree: 0,
			fileSize: f.size ?? 0,
			tags: opts.includeTags ? [...new Set(f.tags ?? [])] : [],
			unresolved: false,
			tag: false,
		});
	}

	const links: GraphLink[] = [];
	const addLink = (si: number, ti: number) => {
		links.push({ source: si, target: ti });
		const s = nodes[si];
		const t = nodes[ti];
		if (s) {
			s.degree++;
			s.outDegree++;
		}
		if (t) {
			t.degree++;
			t.inDegree++;
		}
	};

	for (const src of Object.keys(resolvedLinks)) {
		const si = indexById.get(src);
		if (si === undefined) continue;
		const targets = resolvedLinks[src] ?? {};
		for (const dst of Object.keys(targets)) {
			const ti = indexById.get(dst);
			if (ti === undefined) continue;
			addLink(si, ti);
		}
	}

	if (opts.includeUnresolved) {
		for (const src of Object.keys(unresolvedLinks)) {
			const si = indexById.get(src);
			if (si === undefined) continue;
			const targets = unresolvedLinks[src] ?? {};
			for (const name of Object.keys(targets)) {
				const ghostId = `unresolved:${name}`;
				let gi = indexById.get(ghostId);
				if (gi === undefined) {
					gi = nodes.length;
					indexById.set(ghostId, gi);
					nodes.push({
						id: ghostId,
						name,
						folderTop: '__unresolved__',
						degree: 0,
						inDegree: 0,
						outDegree: 0,
						fileSize: 0,
						tags: [],
						unresolved: true,
						tag: false,
					});
				}
				addLink(si, gi);
			}
		}
	}

	if (opts.includeTags && opts.includeTagHubs) {
		const limit = boundedTagHubLimit(opts.tagHubLimit ?? 20);
		const ranked = topTags({ nodes, links }, limit);
		const hubByTag = new Map<string, number>();
		for (const stat of ranked) {
			const ti = nodes.length;
			hubByTag.set(stat.name, ti);
			indexById.set(stat.id, ti);
			nodes.push({
				id: stat.id,
				name: stat.name,
				folderTop: '__tag__',
				degree: 0,
				inDegree: 0,
				outDegree: 0,
				fileSize: 0,
				tags: [stat.name],
				unresolved: false,
				tag: true,
			});
		}
		// At most limit hubs; the new edges walk only the existing notes' tags, so one rebuild is O(total tag occurrences).
		for (let si = 0; si < nodes.length; si++) {
			const node = nodes[si];
			if (!node || node.tag || node.unresolved) continue;
			for (const rawTag of node.tags) {
				const ti = hubByTag.get(rawTag);
				if (ti !== undefined) addLink(si, ti);
			}
		}
	}

	let result: GraphData = { nodes, links };
	if (!opts.includeOrphans) {
		// Filter the orphans (degree 0): a filtered node necessarily has no edges, so only the indices need reordering
		result = filterNodes(result, (n) => n.degree > 0);
	}
	const nodeCap = opts.nodeCap ?? null;
	if (nodeCap !== null && result.nodes.length > nodeCap) {
		// The top N by degree (ties in the original order) —— keeping the hub structure, "it still looks like this library"
		const ranked = [...result.nodes.entries()].sort((a, b) => b[1].degree - a[1].degree || a[0] - b[0]);
		const keepIdx = new Set(ranked.slice(0, nodeCap).map(([i]) => i));
		result = filterNodes(result, (_n, i) => keepIdx.has(i));
	}
	const linkCap = opts.linkCap ?? null;
	if (linkCap !== null && result.links.length > linkCap) {
		const deg = (i: number) => result.nodes[i]?.degree ?? 0;
		result = {
			nodes: result.nodes,
			links: [...result.links.entries()]
				.sort((a, b) => Math.min(deg(b[1].source), deg(b[1].target)) - Math.min(deg(a[1].source), deg(a[1].target)) || a[0] - b[0])
				.slice(0, linkCap)
				.map(([, l]) => l),
		};
	}
	return result;
}

function filterNodes(g: GraphData, keep: (n: GraphNode, i: number) => boolean): GraphData {
	const remap = new Map<number, number>();
	const kept: GraphNode[] = [];
	g.nodes.forEach((n, i) => {
		if (keep(n, i)) {
			remap.set(i, kept.length);
			kept.push(n);
		}
	});
	const links: GraphLink[] = [];
	for (const l of g.links) {
		const s2 = remap.get(l.source);
		const t2 = remap.get(l.target);
		if (s2 !== undefined && t2 !== undefined) links.push({ source: s2, target: t2 });
	}
	return { nodes: kept, links };
}
