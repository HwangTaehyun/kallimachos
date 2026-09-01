import { describe, it, expect } from 'vitest';
import { groupIndices } from '../src/layout/groupIndex';
import type { GraphData, GraphNode } from '../src/types';

const node = (folderTop: string): GraphNode => ({
	id: `kdb:${folderTop}`, name: folderTop, folderTop,
	degree: 1, inDegree: 0, outDegree: 1, fileSize: 0,
	tags: [], unresolved: false, tag: false,
});
const data = (tops: string[]): GraphData => ({ nodes: tops.map(node), links: [] });

/** The index community cohesion (forceCommunity) reads.  Wrong here and the wrong nodes clump. */
describe('groupIndices', () => {
	it('the same group gets the same index, a different group a different one', () => {
		const g = groupIndices(data(['A', 'B', 'A', 'C', 'B']));
		expect(g[0]).toBe(g[2]);
		expect(g[1]).toBe(g[4]);
		expect(new Set([g[0], g[1], g[3]]).size).toBe(3);
	});

	it("'other' is -1 —— unrelated things have no reason to clump", () => {
		const g = groupIndices(data(['A', 'Other', 'B', 'Other']));
		expect(g[1]).toBe(-1);
		expect(g[3]).toBe(-1);
		expect(g[0]).toBeGreaterThanOrEqual(0);
	});

	it("an empty string is -1 too (the note graph's root folder)", () => {
		expect(groupIndices(data(['', 'A']))[0]).toBe(-1);
	});

	it('the indices run from 0 with no gaps —— forceCommunity uses them as an array size', () => {
		const g = groupIndices(data(['X', 'Y', 'Z', 'X']));
		const used = [...new Set([...g].filter((v) => v >= 0))].sort((a, b) => a - b);
		expect(used).toEqual([0, 1, 2]);
	});

	it('the length matches the node count', () => {
		expect(groupIndices(data(['A', 'B', 'C'])).length).toBe(3);
		expect(groupIndices(data([])).length).toBe(0);
	});
});
