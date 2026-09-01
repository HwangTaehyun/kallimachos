import { describe, expect, it } from 'vitest';
import { LANGS, setLang, t } from '../src/i18n';
import { EN } from '../src/i18n/en';
import { ZH } from '../src/i18n/zh';
import { DE } from '../src/i18n/de';
import { IT } from '../src/i18n/it';
import { ES } from '../src/i18n/es';
import { PT } from '../src/i18n/pt';

/** Every place a counted value goes —— t() fills only what is in the string. */
const COUNTS = { path: 'note', n: 0, in: 0, out: 0, total: 9, d: 0, v: 0, msg: 'x' };

describe('{var|singular|plural}', () => {
	it('English distinguishes 1 from 2', () => {
		setLang('en');
		expect(t('search.hit', { path: 'note', n: 1 })).toBe('note · 1 link');
		expect(t('search.hit', { path: 'note', n: 2 })).toBe('note · 2 links');
		expect(t('card.stats', { in: 1, out: 1 })).toBe('↩ 1 backlink · → 1 outgoing');
	});

	it('Chinese, which has no plural marker, is the same whatever the count', () => {
		setLang('zh');
		expect(t('search.hit', { path: 'note', n: 1 })).toBe(t('search.hit', { path: 'note', n: 2 }).replace('2', '1'));
	});

	// A typo in the marker (a missing closing brace, say) leaks `{` and `|` straight onto the screen —— all 6 languages are swept.
	it('the marker never leaks to the screen, in any language or at any count', () => {
		const dicts = { en: EN, zh: ZH, de: DE, it: IT, es: ES, pt: PT };
		for (const { code } of LANGS) {
			setLang(code);
			for (const key of Object.keys(EN) as (keyof typeof EN)[]) {
				if (!dicts[code][key].includes('|')) continue;
				for (const n of [1, 2]) {
					const out = t(key, { ...COUNTS, n, in: n, out: n });
					expect(`${code}/${key}/${n}: ${out}`).not.toMatch(/[{}|]/);
				}
			}
		}
	});
});
