import type { GraphData } from '../types';

/** The groups to leave out of community cohesion.  They are unrelated to each other and have no reason to clump. */
/** The bin holding what was too small to count as a community.  Not a real community ——
    it has to be left out of highlighting and hover (see MIN_COMM in export_kdb_graph.py). */
export const OTHER_GROUP = 'Other';
const OTHER = OTHER_GROUP;

/**
 * folderTop (the group name) → a 0..N-1 index.  'other' and empty are -1.
 *
 * In community mode folderTop is the community name (kdbGraph.groupOf).  The layout does not need
 * the name and only needs "is this the same group", so it is folded into an integer here.
 * Sending it to the Worker, one Int32Array is also cheaper than an array of strings.
 */
export function groupIndices(data: GraphData): Int32Array {
	const out = new Int32Array(data.nodes.length).fill(-1);
	const id = new Map<string, number>();
	data.nodes.forEach((n, i) => {
		const g = n.folderTop;
		if (!g || g === OTHER) return;
		let k = id.get(g);
		if (k === undefined) { k = id.size; id.set(g, k); }
		out[i] = k;
	});
	return out;
}

