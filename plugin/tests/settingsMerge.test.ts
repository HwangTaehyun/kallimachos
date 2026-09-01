import { describe, expect, it } from 'vitest';
import { DEFAULT_SETTINGS, mergeSettings } from '../src/settings';

describe('mergeSettings v0.4 compatibility (curves + the deep-space background)', () => {
	it('an empty save → the new defaults (the galaxy preset including curves and the background layers)', () => {
		const m = mergeSettings({});
		expect(m.look.linkCurve).toBe(DEFAULT_SETTINGS.look.linkCurve);
		expect(m.space).toEqual(DEFAULT_SETTINGS.space);
	});

	it('a pre-v0.4 save: look missing linkCurve and no space → filled with the defaults', () => {
		const m = mergeSettings({ look: { nodeSize: 1.5, linkOpacity: 0.2, twinkle: 0, sizeBy: 'uniform' } });
		expect(m.look.nodeSize).toBe(1.5);
		expect(m.look.linkCurve).toBe(DEFAULT_SETTINGS.look.linkCurve);
		expect(m.space).toEqual(DEFAULT_SETTINGS.space);
	});

	it('an old custom preset missing the new fields → filled with 0 (keeping the straight-line, no-background look of when it was saved)', () => {
		const m = mergeSettings({
			customPresets: [
				{
					id: 'custom-old',
					name: '我的 1',
					starfield: true,
					theme: 'hubble',
					bloom: { strength: 0.3, radius: 0.3, threshold: 0.2 },
					physics: { repel: 100, linkDistance: 50, linkStrength: 1, centerPull: 0.05, flatten: 0, coreGravity: 0, spiral: 0 },
					look: { nodeSize: 1, linkOpacity: 0.1, twinkle: 0.5, sizeBy: 'degree' },
				},
			],
		});
		const p = m.customPresets[0]!;
		expect(p.look.linkCurve).toBe(0);
		expect(p.space).toEqual({ nebula: 0, fieldStars: 0, clusterClouds: 0 });
		expect(p.look.nodeSize).toBe(1); // the original fields are untouched
	});

	it('a save carrying the new fields is kept as-is', () => {
		const m = mergeSettings({ space: { nebula: 0.9, fieldStars: 0, clusterClouds: 0.1 }, look: { linkCurve: 0.7 } });
		expect(m.space).toEqual({ nebula: 0.9, fieldStars: 0, clusterClouds: 0.1 });
		expect(m.look.linkCurve).toBe(0.7);
	});

	// A renamed name has to survive one "save → restart" round trip (loadData → mergeSettings is the restart read path)
	it('mergeSettings keeps name/nameEn after a custom preset is renamed (the name survives a restart)', () => {
		const saved = {
			customPresets: [
				{
					id: 'custom-x',
					name: '我的深空调色',
					nameEn: '我的深空调色',
					starfield: true,
					theme: 'hubble',
					space: { nebula: 0.5, fieldStars: 0.3, clusterClouds: 0.2 },
					bloom: { strength: 0.3, radius: 0.3, threshold: 0.2 },
					physics: { repel: 100, linkDistance: 50, linkStrength: 1, centerPull: 0.05, flatten: 0, coreGravity: 0, spiral: 0 },
					look: { nodeSize: 1, linkOpacity: 0.1, linkCurve: 0.4, twinkle: 0.5, sizeBy: 'degree' },
				},
			],
		};
		// The serialisation round trip simulates writing to and reading back from disk
		const roundTrip = mergeSettings(JSON.parse(JSON.stringify(saved)));
		expect(roundTrip.customPresets[0]!.name).toBe('我的深空调色');
		expect(roundTrip.customPresets[0]!.nameEn).toBe('我的深空调色');
	});
});

describe('mergeSettings v0.5 compatibility (note filtering, #11)', () => {
	it('a 0.4.x save with no filterQuery → an empty string (no filtering), so an existing user does not open the graph with fewer notes', () => {
		const m = mergeSettings({ showTags: true, showOrphans: false });
		expect(m.filterQuery).toBe('');
		expect(m.showTags).toBe(true); // the existing fields are unaffected
		expect(m.showOrphans).toBe(false);
	});

	it('a filterQuery in the save is kept as-is (quotes and negation included)', () => {
		const m = mergeSettings({ filterQuery: '-file:"Index" path:Daily' });
		expect(m.filterQuery).toBe('-file:"Index" path:Daily');
	});

	it('a filterQuery of the wrong type → falls back to the default rather than putting a non-string in', () => {
		expect(mergeSettings({ filterQuery: 42 }).filterQuery).toBe('');
		expect(mergeSettings({ filterQuery: null }).filterQuery).toBe('');
	});
});

describe('mergeSettings Tag Lens', () => {
	it('an old save has no Lens by default; a new save keeps the tag id', () => {
		expect(mergeSettings({ showTags: true }).tagLens).toBeNull();
		expect(mergeSettings({ showTags: true, tagLens: 'tag:#shared' }).tagLens).toBe('tag:#shared');
	});

	it('a non-string persisted value falls back to null', () => {
		expect(mergeSettings({ tagLens: 42 }).tagLens).toBeNull();
		expect(mergeSettings({ tagLens: false }).tagLens).toBeNull();
	});

	it('an old save is filled with the tag colours and hubs off by default, and the default cap', () => {
		const m = mergeSettings({ showTags: true });
		expect(m.colorByTag).toBe(false);
		expect(m.showTagHubs).toBe(false);
		expect(m.tagHubLimit).toBe(20);
	});

	it('the new settings persist; the hub limit is clamped to 5–50', () => {
		expect(mergeSettings({ colorByTag: true, showTagHubs: true, tagHubLimit: 37 })).toMatchObject({
			colorByTag: true,
			showTagHubs: true,
			tagHubLimit: 37,
		});
		expect(mergeSettings({ tagHubLimit: 1 }).tagHubLimit).toBe(5);
		expect(mergeSettings({ tagHubLimit: 99 }).tagHubLimit).toBe(50);
		expect(mergeSettings({ tagHubLimit: 'many' }).tagHubLimit).toBe(20);
	});
});
