import { describe, it, expect } from 'vitest';
import { mergeSettings, DEFAULT_SETTINGS } from '../src/settings';

/**
 * The regression test for filter namespace separation.
 *
 * What it prevents —— a defect from the 2026-08-18 adversarial review.  Storing the switched-off
 * groups in **one** `hiddenFolders` array meant that quitting in community mode with a community
 * switched off, then opening in note mode, **compared that community name against folder names**.
 * The result was either a ghost filter hiding nothing, or a folder that happened to share the
 * name being hidden without a word.  Either way the user sees "the filter is broken".
 */
describe('hiddenByNamespace', () => {
	it('the default has all three namespaces', () => {
		expect(DEFAULT_SETTINGS.hiddenByNamespace).toEqual({ notes: [], type: [], community: [] });
	});

	it('restores a saved value per namespace', () => {
		const s = mergeSettings({
			hiddenByNamespace: { notes: ['wiki'], type: ['concept'], community: ['하이브리드 검색 시스템'] },
		});
		expect(s.hiddenByNamespace.notes).toEqual(['wiki']);
		expect(s.hiddenByNamespace.type).toEqual(['concept']);
		expect(s.hiddenByNamespace.community).toEqual(['하이브리드 검색 시스템']);
	});

	it('the namespaces do not mix', () => {
		const s = mergeSettings({
			hiddenByNamespace: { notes: [], type: ['concept'], community: ['군집A'] },
		});
		expect(s.hiddenByNamespace.type).not.toContain('군집A');
		expect(s.hiddenByNamespace.community).not.toContain('concept');
	});

	it('an old settings file (with no hiddenByNamespace) does not break', () => {
		const s = mergeSettings({ hiddenFolders: ['wiki'] });
		expect(s.hiddenFolders).toEqual(['wiki']);
		expect(s.hiddenByNamespace).toEqual({ notes: [], type: [], community: [] });
	});

	it('a corrupted value falls back to an empty array', () => {
		for (const bad of [null, 'nope', 42, { notes: 'x' }, { notes: [1, 2] }]) {
			const s = mergeSettings({ hiddenByNamespace: bad });
			expect(s.hiddenByNamespace.notes).toEqual([]);
			expect(Array.isArray(s.hiddenByNamespace.community)).toBe(true);
		}
	});
});
