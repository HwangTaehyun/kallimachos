import { describe, it, expect } from 'vitest';
import { NAV, DEFAULT_ITEM, filterNav, findItem } from '../src/settingsNav';

/**
 * The contents list and its search.
 *
 * A search that looks at **the label alone** may as well not exist —— someone typing "bm25" does
 * not know it lives inside "Chunking · Search".  Knowing that is search's job.
 */
describe('settingsNav', () => {
	it('an empty query gives the contents list unchanged', () => {
		expect(filterNav('')).toBe(NAV);
		expect(filterNav('   ')).toBe(NAV);
	});

	it('the default item really is in the contents list', () => {
		//  A typo in DEFAULT_ITEM gives **a blank screen** when settings opens.
		expect(findItem(DEFAULT_ITEM)).toBeTruthy();
	});

	it('every item id is unique', () => {
		const ids = NAV.flatMap((g) => g.items.map((i) => i.id));
		expect(new Set(ids).size).toBe(ids.length);
	});

	it('it finds things by words outside the label (keywords)', () => {
		//  ★ This is the point.  'bm25' is in no label at all.
		const hit = filterNav('bm25');
		expect(hit.flatMap((g) => g.items.map((i) => i.id))).toEqual(['tuning']);
	});

	it('the old name is still findable', () => {
		//  The group was renamed from "Index" to "Knowledge DB" (2026-08-23).  A rename makes
		//  **someone searching by the old word suddenly unable to find it** —— that is a rename's
		//  real cost.  A word dropped from a label is kept in keywords.
		//  '색인' stays Korean: it is the old label kept in settingsNav's ko keywords, and
		//  searching for it is exactly what this test is about.
		const hit = filterNav('색인').flatMap((g) => g.items.map((i) => i.id));
		expect(hit).toContain('tuning');
		expect(hit).toContain('paths');
	});

	it('it ignores case', () => {
		expect(filterNav('BM25')).toEqual(filterNav('bm25'));
	});

	it('it finds things by the group name', () => {
		expect(filterNav('Knowledge graph').flatMap((g) => g.items.map((i) => i.id)))
			.toEqual(['aliases', 'homonyms']);
	});

	it('an empty group disappears, title and all', () => {
		//  A title left on its own reads as "it is here but I cannot see it", not "it is not here".
		const hit = filterNav('bm25');
		expect(hit).toHaveLength(1);
		expect(hit.every((g) => g.items.length > 0)).toBe(true);
	});

	it('an empty array when nothing matches', () => {
		expect(filterNav('nosuchsettingxyz')).toEqual([]);
	});

	it('an unknown id gives undefined', () => {
		expect(findItem('nosuchid')).toBeUndefined();
	});
});
