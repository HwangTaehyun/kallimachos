import { describe, expect, it } from 'vitest';
import { assignFolderHues, fallbackColorFn, folderColor, folderCoveredByGroups, makeTagColorFn } from '../src/render/palette';
import type { GraphNode } from '../src/types';

// Note: three's setHSL computes in linear space and getHexString converts back to sRGB, so the values are not a naive HSL→RGB result
const hex = (folder: string) => `#${folderColor(folder, false).getHexString()}`;

/**
 * The regression case comes from Rick's real library (3,224 notes / 14 top-level folders / 9 colour groups imported from the 2D graph).
 * Before the fix: the fallback hue = HUES[hash32(folder) % 9], unrelated to folder size and unordered →
 * 99Archive (545) / 90Oldpapers (86) / Readwise (68) collided on the same blue, 1,184 notes = 37% of the library reading identically.
 *
 * The folder names below stay Chinese: they are Rick's real folders, and the collision this test
 * pins down is a property of those exact strings' hashes.
 */
const REAL_FOLDERS = [
	{ folder: '04AI', count: 607 },
	{ folder: '99Archive', count: 545 },
	{ folder: 'Cubox', count: 505 },
	{ folder: '80随记', count: 392 },
	{ folder: '02工作', count: 284 },
	{ folder: '01学习', count: 197 },
	{ folder: '05读书', count: 133 },
	{ folder: '30认真活着', count: 113 },
	{ folder: '60流浪', count: 93 },
	{ folder: '06人', count: 92 },
	{ folder: '90故纸堆', count: 86 },
	{ folder: 'Readwise', count: 68 },
	{ folder: '03产品', count: 58 },
	{ folder: '00Meta', count: 51 },
];
const REAL_GROUPS = [
	{ query: 'path:01学习', color: '#46d4dc' },
	{ query: 'path:02工作', color: '#ffc35c' },
	{ query: 'path:03产品', color: '#d05a32' },
	{ query: 'path:04AI', color: '#7fd0a0' },
	{ query: 'path:05读书', color: '#e8d9a0' },
	{ query: 'path:06人', color: '#5a9bd8' },
	{ query: 'path:30认真活着', color: '#d87fa8' },
	{ query: 'path:Cubox', color: '#9a7fe0' },
	{ query: 'path:00Meta', color: '#cfd8e8' },
];
const ranked = REAL_FOLDERS.map((f) => f.folder);
const covered = (f: string) => folderCoveredByGroups(f, REAL_GROUPS);

describe('folderCoveredByGroups', () => {
	it('recognises a folder covered by a path: group', () => {
		expect(covered('04AI')).toBe(true);
		expect(covered('Cubox')).toBe(true);
	});

	it('a folder with no matching group = uncovered (these 5 are exactly the ones needing a fallback hue)', () => {
		for (const f of ['99Archive', '80随记', '60流浪', '90故纸堆', 'Readwise']) {
			expect(covered(f), f).toBe(false);
		}
	});
});

describe('assignFolderHues (the collision fix)', () => {
	it("Rick's real library: the 5 uncovered folders get 5 different colours", () => {
		assignFolderHues(ranked, covered);
		const fallback = ['99Archive', '80随记', '60流浪', '90故纸堆', 'Readwise'];
		const colors = fallback.map(hex);
		expect(new Set(colors).size).toBe(fallback.length);
	});

	it('the three folders that collided on one blue before the fix are now pairwise different', () => {
		assignFolderHues(ranked, covered);
		expect(hex('99Archive')).not.toBe(hex('90故纸堆'));
		expect(hex('90故纸堆')).not.toBe(hex('Readwise'));
		expect(hex('99Archive')).not.toBe(hex('Readwise'));
	});

	it('a folder covered by a colour group takes no hue slot —— otherwise the fallbacks get pushed back into a collision', () => {
		assignFolderHues(ranked, covered);
		// The uncovered ones take HUES[0..4] = 0/40/80/120/160 in rank order
		expect(hex('99Archive')).toBe('#eca2a2'); // rank 1 (the largest uncovered) → hue 0
		expect(hex('80随记')).toBe('#ecd7a2'); // → hue 40 (the folder name is fixture data, see the header)
	});

	it('with ≤ 9 folders none of them collide', () => {
		const nine = Array.from({ length: 9 }, (_, i) => `F${i}`);
		assignFolderHues(nine, () => false);
		expect(new Set(nine.map(hex)).size).toBe(9);
	});

	it('past 9 folders awaiting a hue it recycles, but what collides is the smallest few rather than the largest', () => {
		const twelve = Array.from({ length: 12 }, (_, i) => `F${i}`); // already descending by size
		assignFolderHues(twelve, () => false);
		const top9 = twelve.slice(0, 9).map(hex);
		expect(new Set(top9).size).toBe(9); // the top 9 are guaranteed not to collide
		expect(hex('F9')).toBe(hex('F0')); // recycling of the wheel starts at the 10th
	});

	it('根目录（空串）不占色相槽', () => {
		assignFolderHues(['', 'A', 'B'], () => false);
		expect(hex('A')).toBe('#eca2a2'); // A 仍拿 hue 0，没被空串挤掉
	});

	it('重发色相会清掉旧缓存，不留上一次的颜色', () => {
		assignFolderHues(['X', 'Y'], () => false);
		const before = hex('Y'); // hue 40
		assignFolderHues(['Y', 'X'], () => false); // Y 升到第一
		expect(hex('Y')).not.toBe(before);
		expect(hex('Y')).toBe('#eca2a2'); // hue 0
	});
});

describe('makeTagColorFn', () => {
	const note = (id: string, tags: string[]): GraphNode => ({
		id,
		name: id,
		folderTop: 'Folder',
		degree: 0,
		inDegree: 0,
		outDegree: 0,
		fileSize: 0,
		tags,
		unresolved: false,
		tag: false,
	});

	it('按第一个标签稳定着色；同标签同色，不同标签不同色', () => {
		const color = makeTagColorFn(fallbackColorFn);
		expect(color(note('a.md', ['#shared', '#other'])).getHex()).toBe(color(note('b.md', ['#shared'])).getHex());
		expect(color(note('a.md', ['#shared'])).getHex()).not.toBe(color(note('c.md', ['#different'])).getHex());
	});

	it('无标签笔记回落既有文件夹配色', () => {
		const node = note('plain.md', []);
		expect(makeTagColorFn(fallbackColorFn)(node).getHex()).toBe(fallbackColorFn(node).getHex());
	});
});
