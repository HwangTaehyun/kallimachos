import { describe, expect, it } from 'vitest';
import { buildGraph } from '../src/data/buildGraph';
import { mergeResolvedLinks, parseCanvasFileLinks } from '../src/data/canvasLinks';

const files = [
	{ path: '01学习/笔记A.md', basename: '笔记A' },
	{ path: '01学习/子目录/笔记B.md', basename: '笔记B' },
	{ path: '02工作/笔记C.md', basename: '笔记C' },
	{ path: '根笔记.md', basename: '根笔记' },
];

describe('buildGraph', () => {
	it('nodes: the path is the id, grouped by top-level folder, the root an empty string', () => {
		const g = buildGraph(files, {}, {}, { includeUnresolved: false, includeOrphans: true });
		expect(g.nodes).toHaveLength(4);
		expect(g.nodes[0]).toMatchObject({ id: '01学习/笔记A.md', folderTop: '01学习', unresolved: false });
		expect(g.nodes[1]?.folderTop).toBe('01学习');
		expect(g.nodes[3]?.folderTop).toBe('');
	});

	it('edges: by index, degree = out + in; a target outside the md set (an attachment, or nonexistent) is dropped', () => {
		const resolved = {
			'01学习/笔记A.md': { '01学习/子目录/笔记B.md': 3, '附件/图.png': 1 },
			'02工作/笔记C.md': { '01学习/笔记A.md': 1 },
			'幽灵来源.md': { '01学习/笔记A.md': 1 },
		};
		const g = buildGraph(files, resolved, {}, { includeUnresolved: false, includeOrphans: true });
		expect(g.links).toEqual([
			{ source: 0, target: 1 },
			{ source: 2, target: 0 },
		]);
		expect(g.nodes[0]?.degree).toBe(2);
		expect(g.nodes[1]?.degree).toBe(1);
		expect(g.nodes[2]?.degree).toBe(1);
		expect(g.nodes[3]?.degree).toBe(0);
	});

	it('the same pair appearing several times in resolvedLinks (the value = the count) makes one edge', () => {
		const g = buildGraph(files, { '01学习/笔记A.md': { '02工作/笔记C.md': 99 } }, {}, { includeUnresolved: false, includeOrphans: true });
		expect(g.links).toHaveLength(1);
	});

	it('Canvas as an ordinary node, accepting the Canvas source and target edges already in resolvedLinks', () => {
		const withCanvas = [
			{ path: 'notes/a.md', basename: 'a' },
			{ path: 'boards/plan.canvas', basename: 'plan' },
			{ path: 'notes/b.md', basename: 'b' },
		];
		const parsed = parseCanvasFileLinks(JSON.stringify({
			nodes: [{ type: 'file', file: 'notes/a.md' }],
		}));
		expect(parsed).not.toBeNull();
		const resolved = mergeResolvedLinks({ 'notes/b.md': { 'boards/plan.canvas': 1 } }, {
			'boards/plan.canvas': parsed ?? {},
		});

		const g = buildGraph(withCanvas, resolved, {}, { includeUnresolved: false, includeOrphans: true });

		expect(g.nodes.map((node) => node.id)).toEqual(['notes/a.md', 'boards/plan.canvas', 'notes/b.md']);
		expect(g.links).toEqual([
			{ source: 2, target: 1 },
			{ source: 1, target: 0 },
		]);
		expect(g.nodes[1]).toMatchObject({ name: 'plan', degree: 2, inDegree: 1, outDegree: 1, unresolved: false, tag: false });
	});

	it('unresolved: with the switch on it makes ghost nodes and deduplicates across sources', () => {
		const unresolved = {
			'01学习/笔记A.md': { 概念词典: 2 },
			'02工作/笔记C.md': { 概念词典: 1, 另一个幽灵: 1 },
		};
		const off = buildGraph(files, {}, unresolved, { includeUnresolved: false, includeOrphans: true });
		expect(off.nodes).toHaveLength(4);
		expect(off.links).toHaveLength(0);

		const on = buildGraph(files, {}, unresolved, { includeUnresolved: true, includeOrphans: true });
		const ghosts = on.nodes.filter((n) => n.unresolved);
		expect(ghosts).toHaveLength(2);
		expect(ghosts[0]).toMatchObject({ id: 'unresolved:概念词典', folderTop: '__unresolved__' });
		expect(on.links).toHaveLength(3);
		expect(on.nodes.find((n) => n.id === 'unresolved:概念词典')?.degree).toBe(2);
	});

	it('an empty vault does not blow up', () => {
		const g = buildGraph([], {}, {}, { includeUnresolved: true, includeOrphans: true });
		expect(g.nodes).toHaveLength(0);
		expect(g.links).toHaveLength(0);
	});
});

describe('orphan filtering', () => {
	it('includeOrphans=false removes zero-degree nodes and reindexes the edges', () => {
		const resolved = { '01学习/笔记A.md': { '02工作/笔记C.md': 1 } };
		const g = buildGraph(files, resolved, {}, { includeUnresolved: false, includeOrphans: false });
		expect(g.nodes.map((n) => n.name)).toEqual(['笔记A', '笔记C']);
		expect(g.links).toEqual([{ source: 0, target: 1 }]);
	});

	it('fileSize passes through from FileRecord.size', () => {
		const sized = [{ path: 'a.md', basename: 'a', size: 12345 }];
		const g = buildGraph(sized, {}, {}, { includeUnresolved: false, includeOrphans: true });
		expect(g.nodes[0]?.fileSize).toBe(12345);
	});
});

describe('the quality-tier caps (M4)', () => {
	it('nodeCap takes the top N by degree and reindexes; linkCap truncates by min(endpoint degree)', () => {
		const resolved = {
			'01学习/笔记A.md': { '01学习/子目录/笔记B.md': 1, '02工作/笔记C.md': 1, '根笔记.md': 1 },
			'01学习/子目录/笔记B.md': { '02工作/笔记C.md': 1 },
		};
		// Degrees: A=3, B=2, C=2, root=1
		const capped = buildGraph(files, resolved, {}, { includeUnresolved: false, includeOrphans: true, nodeCap: 3 });
		expect(capped.nodes.map((n) => n.name)).toEqual(['笔记A', '笔记B', '笔记C']);
		expect(capped.links).toHaveLength(3); // A-root is dropped
		const linkCapped = buildGraph(files, resolved, {}, { includeUnresolved: false, includeOrphans: true, linkCap: 2 });
		expect(linkCapped.links).toHaveLength(2);
		// What is kept is the edges with the highest min degree: A-B (min 2), A-C (min 2); B-C? min(B,C)=2, a tie broken by the original order —— what is dropped is A-root (min 1)
		expect(linkCapped.links.every((l) => l.source !== 3 && l.target !== 3)).toBe(true);
	});
});

describe('tag data and bounded hubs', () => {
	const taggedFiles = [
		{ path: 'a.md', basename: 'a', tags: ['#课堂笔记', '#数学'] },
		{ path: 'b.md', basename: 'b', tags: ['#课堂笔记'] },
	];

	it('includeTags passes through the deduplicated note tags only, making no hidden nodes or layout edges', () => {
		const g = buildGraph(taggedFiles, {}, {}, { includeUnresolved: false, includeOrphans: true, includeTags: true });
		expect(g.nodes).toHaveLength(2);
		expect(g.links).toHaveLength(0);
		expect(g.nodes[0]?.tags).toEqual(['#课堂笔记', '#数学']);
		expect(g.nodes.every((n) => !n.tag)).toBe(true);
	});

	it('includeTags=false: neither tag data nor hubs', () => {
		const g = buildGraph(taggedFiles, {}, {}, { includeUnresolved: false, includeOrphans: true, includeTags: false });
		expect(g.nodes.every((n) => !n.tag)).toBe(true);
		expect(g.nodes.every((n) => n.tags.length === 0)).toBe(true);
		expect(g.nodes).toHaveLength(2);
	});

	it('showTags + hubs makes only the top 5 hubs; a repeated tag on the same note makes no duplicate edge', () => {
		const many = [
			{ path: 'a.md', basename: 'a', tags: ['#x', '#x', '#a', '#b', '#c', '#d', '#e'] },
			{ path: 'b.md', basename: 'b', tags: ['#x'] },
		];
		const g = buildGraph(many, {}, {}, {
			includeUnresolved: false,
			includeOrphans: true,
			includeTags: true,
			includeTagHubs: true,
			tagHubLimit: 5,
		});
		const hubs = g.nodes.filter((n) => n.tag);
		expect(hubs).toHaveLength(5);
		expect(hubs[0]).toMatchObject({ id: 'tag:#x', tags: ['#x'], degree: 2 });
		expect(g.links.filter((l) => g.nodes[l.target]?.id === 'tag:#x')).toHaveLength(2);
		expect(g.nodes.some((n) => n.id === 'tag:#e')).toBe(false);
	});

	it('includeTagHubs alone has no effect; hubs must pass the showTags master switch', () => {
		const g = buildGraph(taggedFiles, {}, {}, {
			includeUnresolved: false,
			includeOrphans: true,
			includeTags: false,
			includeTagHubs: true,
			tagHubLimit: 50,
		});
		expect(g.nodes.every((n) => !n.tag && n.tags.length === 0)).toBe(true);
	});

	it('a corrupted or old save giving an enormous limit is still hard-capped at 50 hubs by the build side', () => {
		const many = Array.from({ length: 60 }, (_, i) => ({ path: `${i}.md`, basename: `${i}`, tags: [`#t${i}`] }));
		const g = buildGraph(many, {}, {}, {
			includeUnresolved: false,
			includeOrphans: true,
			includeTags: true,
			includeTagHubs: true,
			tagHubLimit: 999,
		});
		expect(g.nodes.filter((n) => n.tag)).toHaveLength(50);
	});
});
