import { describe, it, expect } from 'vitest';
import type { Dict, Strings } from '../src/i18n/lang';

//  The invariants of the per-component dictionaries (src/i18n/strings/*.ts).  Scattered across 19
//  dictionaries, a human eye cannot catch a missing key —— a key missing from ko makes the screen
//  quietly fall back to English (Loose allows it) and nobody knows.  Added with the language setting on 2026-09-01.
const modules = import.meta.glob<{ default: Dict<Strings> }>('../src/i18n/strings/*.ts', { eager: true });
const entries = Object.entries(modules).map(([path, m]) => [path.split('/').pop()!, m.default] as const);

describe('i18n strings', () => {
	it('the dictionary modules exist (so the glob is not running on nothing)', () => {
		expect(entries.length).toBeGreaterThan(10);
	});

	it.each(entries)('%s: ko has every one of en’s keys and no extras', (_name, d) => {
		const en = Object.keys(d.en).sort(), ko = Object.keys(d.ko).sort();
		expect(ko).toEqual(en);
	});

	it.each(entries)('%s: no Korean in an en value, and the value kind (string/function) matches between en and ko', (_name, d) => {
		for (const [k, v] of Object.entries(d.en)) {
			if (typeof v === 'string') expect(v, `en.${k}`).not.toMatch(/[가-힣]/);
			expect(typeof d.ko[k], `ko.${k}`).toBe(typeof v);
		}
	});
});
