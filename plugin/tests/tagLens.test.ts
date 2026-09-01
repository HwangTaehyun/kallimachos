import { describe, expect, it } from 'vitest';
import { buildAdjacency } from '../src/data/Adjacency';
import { buildGraph } from '../src/data/buildGraph';
import { boundedTagHubLimit, primaryTag, resolveTagLens, tagLensFocus, toggleTagLens, topTags } from '../src/data/tagLens';

function taggedGraph(hubs = false) {
	return buildGraph(
		[
			{ path: 'a.md', basename: 'a', tags: ['#shared', '#alpha'] },
			{ path: 'b.md', basename: 'b', tags: ['#shared', '#beta'] },
			{ path: 'c.md', basename: 'c', tags: ['#shared', '#beta'] },
		],
		{ 'a.md': { 'b.md': 1 } },
		{},
		{ includeUnresolved: false, includeOrphans: true, includeTags: true, includeTagHubs: hubs, tagHubLimit: 5 },
	);
}

describe('topTags', () => {
	it('returns the top N by tag occurrence across the notes, independent of hub nodes', () => {
		const tags = topTags(taggedGraph(), 2);
		expect(tags).toEqual([
			{ id: 'tag:#shared', name: '#shared', count: 3 },
			{ id: 'tag:#beta', name: '#beta', count: 2 },
		]);
	});

	it('at most 12 tags by default', () => {
		const files = Array.from({ length: 14 }, (_, i) => ({ path: `${i}.md`, basename: `${i}`, tags: [`#t${i}`] }));
		const graph = buildGraph(files, {}, {}, { includeUnresolved: false, includeOrphans: true, includeTags: true });
		expect(topTags(graph)).toHaveLength(12);
	});

	it('an active tag that falls out of the top 12 is appended, keeping its clear-it entry point', () => {
		const files = Array.from({ length: 14 }, (_, i) => ({ path: `${i}.md`, basename: `${i}`, tags: [`#t${i}`] }));
		const graph = buildGraph(files, {}, {}, { includeUnresolved: false, includeOrphans: true, includeTags: true });
		const tags = topTags(graph, 12, 'tag:#t9');
		expect(tags).toHaveLength(13);
		expect(tags[12]?.id).toBe('tag:#t9');
	});
});

describe('tagLensFocus', () => {
	it('with hubs off it still includes the matching notes and the real links between them', () => {
		const graph = taggedGraph();
		const lens = tagLensFocus(graph, buildAdjacency(graph), 'tag:#shared');
		expect(lens).not.toBeNull();
		const ids = [...(lens?.nodeIndices ?? [])].map((index) => graph.nodes[index]?.id);
		expect(new Set(ids)).toEqual(new Set(['a.md', 'b.md', 'c.md']));
		expect(lens?.tagIndex).toBeNull();
		expect(lens?.linkIndices).toEqual([0]); // a → b is a real link between two matching notes
	});

	it('with hubs on it reuses the same Lens and adds the matching hub and its edges', () => {
		const graph = taggedGraph(true);
		const lens = tagLensFocus(graph, buildAdjacency(graph), 'tag:#shared');
		const ids = [...(lens?.nodeIndices ?? [])].map((index) => graph.nodes[index]?.id);
		expect(new Set(ids)).toEqual(new Set(['tag:#shared', 'a.md', 'b.md', 'c.md']));
		expect(lens?.tagIndex).not.toBeNull();
		expect(lens?.linkIndices).toHaveLength(4); // 1 real link + 3 hub edges
	});

	it('returns null when the tag disappears after a rebuild', () => {
		const graph = taggedGraph();
		expect(tagLensFocus(graph, buildAdjacency(graph), 'tag:#missing')).toBeNull();
		expect(tagLensFocus(graph, buildAdjacency(graph), null)).toBeNull();
	});
});

describe('tag helpers', () => {
	it('the hub limit is clamped to 5–50 for an old save or an abnormal value', () => {
		expect(boundedTagHubLimit(-1)).toBe(5);
		expect(boundedTagHubLimit(12.6)).toBe(13);
		expect(boundedTagHubLimit(999)).toBe(50);
	});

	it("primaryTag uses the note's first tag, and a hub uses the tag it stands for", () => {
		const graph = taggedGraph(true);
		expect(primaryTag(graph.nodes.find((n) => n.id === 'a.md')!)).toBe('#shared');
		expect(primaryTag(graph.nodes.find((n) => n.id === 'tag:#shared')!)).toBe('#shared');
	});
});

describe('toggleTagLens', () => {
	it('single select toggles; clicking the current tag again clears it', () => {
		expect(toggleTagLens(null, 'tag:#a')).toBe('tag:#a');
		expect(toggleTagLens('tag:#a', 'tag:#b')).toBe('tag:#b');
		expect(toggleTagLens('tag:#a', 'tag:#a')).toBeNull();
	});
});

describe('resolveTagLens', () => {
	it('turning showTags off, or the tag disappearing after a rebuild, clears the persisted id', () => {
		const graph = taggedGraph();
		const adjacency = buildAdjacency(graph);
		expect(resolveTagLens(graph, adjacency, false, 'tag:#shared')).toEqual({ id: null, focus: null });
		expect(resolveTagLens(graph, adjacency, true, 'tag:#missing')).toEqual({ id: null, focus: null });
	});

	it('with it enabled and the tag still present, the persisted id is kept and the Lens restored', () => {
		const graph = taggedGraph();
		const resolved = resolveTagLens(graph, buildAdjacency(graph), true, 'tag:#shared');
		expect(resolved.id).toBe('tag:#shared');
		expect(resolved.focus?.nodeIndices.size).toBe(3); // the three matching notes are restored with hubs off too
	});
});
