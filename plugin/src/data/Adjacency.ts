import type { GraphData } from '../types';

/**
 * Undirected adjacency (CSR: compressed sparse row) —— three flat typed arrays, with zero per-node object allocation and cache-friendly.
 * Pure data and unit-testable (following buildGraph.ts's convention), rebuilt with every GraphStore.rebuild and keyed on the new indices.
 */
export interface Adjacency {
	/** Length n+1; node i's adjacency entries are in [offset[i], offset[i+1]) */
	offset: Int32Array;
	/** Length 2m; the far node index of each incidence */
	neighbor: Int32Array;
	/** Length 2m; a parallel array: the GraphLink index for that incidence */
	linkOf: Int32Array;
}

export function buildAdjacency(data: GraphData): Adjacency {
	const n = data.nodes.length;
	const m = data.links.length;
	const offset = new Int32Array(n + 1);
	// Counting: each edge adds +1 to both ends (undirected)
	for (const l of data.links) {
		offset[l.source + 1] = (offset[l.source + 1] ?? 0) + 1;
		offset[l.target + 1] = (offset[l.target + 1] ?? 0) + 1;
	}
	// A prefix sum → each node's starting write position
	for (let i = 0; i < n; i++) offset[i + 1] = (offset[i + 1] ?? 0) + (offset[i] ?? 0);
	const neighbor = new Int32Array(2 * m);
	const linkOf = new Int32Array(2 * m);
	const cursor = new Int32Array(n);
	for (let i = 0; i < n; i++) cursor[i] = offset[i] ?? 0;
	for (let li = 0; li < m; li++) {
		const l = data.links[li];
		if (!l) continue;
		const s = l.source;
		const t = l.target;
		const cs = cursor[s] ?? 0;
		neighbor[cs] = t;
		linkOf[cs] = li;
		cursor[s] = cs + 1;
		const ct = cursor[t] ?? 0;
		neighbor[ct] = s;
		linkOf[ct] = li;
		cursor[t] = ct + 1;
	}
	return { offset, neighbor, linkOf };
}

export interface Neighborhood {
	/** Node index → depth (0 = selected / 1 = first degree / 2 = second degree) */
	depthOf: Map<number, number>;
	/** The edge indices directly connected to the selection (first-degree highlighting) */
	linkTier1: number[];
	/** The edge indices reaching the second-degree ring (second-degree highlighting; non-empty only at maxDepth=2) */
	linkTier2: number[];
}

/** The shortest link path between two nodes (unweighted BFS); [] when disconnected, [start] for the same node.  Used by the guided tour. */
export function shortestPath(adj: Adjacency, start: number, end: number): number[] {
	if (start === end) return [start];
	const prev = new Map<number, number>([[start, -1]]);
	const queue = [start];
	let head = 0;
	while (head < queue.length) {
		const u = queue[head++] ?? -1;
		if (u === end) break;
		const s = adj.offset[u] ?? 0;
		const e = adj.offset[u + 1] ?? 0;
		for (let i = s; i < e; i++) {
			const v = adj.neighbor[i] ?? 0;
			if (!prev.has(v)) {
				prev.set(v, u);
				queue.push(v);
			}
		}
	}
	if (!prev.has(end)) return []; // disconnected
	const path: number[] = [];
	let cur: number | undefined = end;
	while (cur !== undefined && cur !== -1) {
		path.push(cur);
		cur = prev.get(cur);
	}
	return path.reverse();
}

/** A depth-limited BFS from the seed (maxDepth 1|2).  Tens of microseconds on 19.5k edges. */
export function neighborhood(adj: Adjacency, seed: number, maxDepth: number): Neighborhood {
	const depthOf = new Map<number, number>([[seed, 0]]);
	const linkTier1: number[] = [];
	const linkTier2: number[] = [];
	let frontier: number[] = [seed];
	for (let d = 1; d <= maxDepth; d++) {
		const next: number[] = [];
		for (const u of frontier) {
			const start = adj.offset[u] ?? 0;
			const end = adj.offset[u + 1] ?? 0;
			for (let e = start; e < end; e++) {
				const v = adj.neighbor[e] ?? 0;
				const li = adj.linkOf[e] ?? 0;
				const dv = depthOf.get(v);
				if (d === 1) linkTier1.push(li);
				else if (dv === undefined || dv === 2) linkTier2.push(li); // an edge reaching the new (second-degree) ring
				if (dv === undefined) {
					depthOf.set(v, d);
					if (d < maxDepth) next.push(v);
				}
			}
		}
		frontier = next;
	}
	return { depthOf, linkTier1, linkTier2 };
}
