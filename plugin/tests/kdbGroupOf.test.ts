import { describe, it, expect } from 'vitest';
import { setExplicitColors, folderColor } from '../src/render/palette';

/**
 * The explicit-colour path.  Without it, 21 communities cycle through HUES's 9 slots and
 * communities 0, 9 and 18 end up the same colour (which is what really happened in the 2026-08-18 review).
 */
describe('setExplicitColors', () => {
	it('uses the assigned colour as-is', () => {
		setExplicitColors(new Map([['하이브리드 검색 시스템', '#eb7a7a']]));
		expect(folderColor('하이브리드 검색 시스템', false).getHexString()).toBe('eb7a7a');
	});

	it('many groups each get a different colour —— a 9-slot rotation would collide here', () => {
		const names = Array.from({ length: 20 }, (_, i) => `group-${i}`);
		const hues = ['#eb7a7a','#7aebc1','#cda5e9','#ebdd7a','#2cd1f2','#a5e9e6','#e9bca5','#96eb7a',
		              '#e12cf2','#2cf266','#f2a52c','#7a9aeb','#eb7ad1','#b7eb7a','#7ad1eb','#eb9a7a',
		              '#9a7aeb','#7aeb9a','#eb7a9a','#d1eb7a'];
		setExplicitColors(new Map(names.map((n, i) => [n, hues[i]!])));
		const got = new Set(names.map((n) => folderColor(n, false).getHexString()));
		expect(got.size).toBe(20);
	});

	it('an unassigned name falls through to the hue-wheel rotation (the old behaviour is kept)', () => {
		setExplicitColors(new Map([['A', '#eb7a7a']]));
		expect(folderColor('없는그룹', false)).toBeDefined();
	});

	it('an empty map clears the assignment —— needed by the type↔community switch', () => {
		setExplicitColors(new Map([['A', '#eb7a7a']]));
		const withExplicit = folderColor('A', false).getHexString();
		setExplicitColors(new Map());
		expect(folderColor('A', false).getHexString()).not.toBe(withExplicit);
	});
});
