import { describe, expect, it } from 'vitest';
import type { GraphData } from '../src/types';
import { buildAdjacency, neighborhood, shortestPath } from '../src/data/Adjacency';

// A chain 0-1-2-3 + one branch 1-4: used to verify the CSR and the depth BFS
//   0 — 1 — 2 — 3
//       |
//       4
function chain(): GraphData {
	return {
		nodes: [0, 1, 2, 3, 4].map((i) => ({
			id: `${i}`,
			name: `${i}`,
			folderTop: '',
			degree: 0,
			inDegree: 0,
			outDegree: 0,
			fileSize: 0,
			tags: [],
			unresolved: false,
			tag: false,
		})),
		links: [
			{ source: 0, target: 1 },
			{ source: 1, target: 2 },
			{ source: 2, target: 3 },
			{ source: 1, target: 4 },
		],
	};
}

describe('buildAdjacency (CSR)', () => {
	it('each undirected edge contributes two incidences; offset is monotonic and n+1 long', () => {
		const adj = buildAdjacency(chain());
		expect(adj.offset).toHaveLength(6); // n+1
		expect(adj.neighbor).toHaveLength(8); // 2m
		expect(adj.linkOf).toHaveLength(8);
		// Node 1's degree = 3 (connected to 0, 2 and 4)
		const deg1 = (adj.offset[2] ?? 0) - (adj.offset[1] ?? 0);
		expect(deg1).toBe(3);
	});

	it('an adjacency entry holds the right far node and the parallel link index', () => {
		const adj = buildAdjacency(chain());
		const start = adj.offset[1] ?? 0;
		const end = adj.offset[2] ?? 0;
		const neigh = new Set<number>();
		for (let e = start; e < end; e++) neigh.add(adj.neighbor[e] ?? -1);
		expect(neigh).toEqual(new Set([0, 2, 4]));
	});

	it('an empty graph does not crash', () => {
		const adj = buildAdjacency({ nodes: [], links: [] });
		expect(adj.offset).toHaveLength(1);
		expect(adj.neighbor).toHaveLength(0);
	});
});

describe('neighborhood (depth-limited BFS)', () => {
	it('depth 1: direct neighbours only, linkTier1 = the selected edges, no tier2', () => {
		const { depthOf, linkTier1, linkTier2 } = neighborhood(buildAdjacency(chain()), 1, 1);
		expect(depthOf.get(1)).toBe(0);
		expect(depthOf.get(0)).toBe(1);
		expect(depthOf.get(2)).toBe(1);
		expect(depthOf.get(4)).toBe(1);
		expect(depthOf.has(3)).toBe(false); // the second degree is not in depth 1
		expect(linkTier1).toHaveLength(3); // 1-0, 1-2, 1-4
		expect(linkTier2).toHaveLength(0);
	});

	it('depth 2: includes the second-degree ring (node 3), tier2 non-empty', () => {
		const { depthOf, linkTier2 } = neighborhood(buildAdjacency(chain()), 1, 2);
		expect(depthOf.get(3)).toBe(2);
		expect(linkTier2.length).toBeGreaterThan(0); // 2-3 reaches the second-degree ring
	});

	it('depth 2 from leaf 0: 0→1 (first degree) →{2,4} (second degree)', () => {
		const { depthOf } = neighborhood(buildAdjacency(chain()), 0, 2);
		expect(depthOf.get(0)).toBe(0);
		expect(depthOf.get(1)).toBe(1);
		expect(depthOf.get(2)).toBe(2);
		expect(depthOf.get(4)).toBe(2);
		expect(depthOf.has(3)).toBe(false); // beyond the third degree
	});
});

describe('shortestPath (guided path BFS)', () => {
	it('the shortest path along the chain 0→3 = [0,1,2,3]', () => {
		expect(shortestPath(buildAdjacency(chain()), 0, 3)).toEqual([0, 1, 2, 3]);
	});

	it('the branch 3→4 goes through hub 1 = [3,2,1,4]', () => {
		expect(shortestPath(buildAdjacency(chain()), 3, 4)).toEqual([3, 2, 1, 4]);
	});

	it('the same node returns one element, disconnected returns empty', () => {
		expect(shortestPath(buildAdjacency(chain()), 2, 2)).toEqual([2]);
		// Add an isolated node 5 (no edges) → disconnected from the rest
		const g = chain();
		g.nodes.push({ id: '5', name: '5', folderTop: '', degree: 0, inDegree: 0, outDegree: 0, fileSize: 0, tags: [], unresolved: false, tag: false });
		expect(shortestPath(buildAdjacency(g), 0, 5)).toEqual([]);
	});
});
