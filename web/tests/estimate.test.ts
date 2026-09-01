/**
 * The estimate display's **fallback rule**.
 *
 * Why this is the place it goes quietly wrong: with no server estimate the screen has to fall back
 * to `Step.minutes`'s fixed value, and a failed fallback raises no error —— it prints `undefinedmin`
 * or `NaNs`, or worse, **a plausible wrong number**.
 */
import { describe, expect, it } from 'vitest';
import { heavy, human, sentence, short } from '../src/estimate';
import type { Estimate, Step } from '../src/api';

const step = (o: Partial<Step> = {}): Step => ({
	id: 'index', title: 'Rebuild the knowledge DB', desc: '', group: 'main', order: 4,
	cmd: ['schema_v3.py'], minutes: 3, needs_llm: false, writes_db: true, ...o,
});
const est = (o: Partial<Estimate> = {}): Estimate => ({
	step: 'index', title: 'Rebuild the knowledge DB', seconds: 18, units: 55, unit: 'chunks',
	basis: '2 of 376 documents changed', source: 'a seed value (too little history)', ...o,
});

describe('human', () => {
	it('under a second it does not state a time —— "0s" reads like a failure', () => {
		expect(human(0)).toBe('instant');
		expect(human(0.4)).toBe('instant');
	});
	it('the unit changes at the boundary', () => {
		expect(human(59)).toBe('59s');
		expect(human(60)).toBe('1min');
		expect(human(3599)).toBe('60min');
		expect(human(3600)).toBe('1.0h');
	});
});

describe('it falls back with no estimate', () => {
	it('short uses the fixed value', () => {
		expect(short(step({ minutes: 30 }), undefined)).toBe('30min');
	});
	it('sentence uses the fixed value', () => {
		expect(sentence(step({ minutes: 30 }), undefined)).toContain('30min');
	});
	it('heavy uses the fixed 5-minute threshold', () => {
		expect(heavy(step({ minutes: 30, writes_db: false }), undefined)).toBe(true);
		expect(heavy(step({ minutes: 1, writes_db: false }), undefined)).toBe(false);
	});
	it('neither undefined nor NaN leaks into the string down any path', () => {
		for (const s of [short, sentence] as const) {
			expect(s(step(), undefined)).not.toMatch(/undefined|NaN/);
			expect(s(step(), est())).not.toMatch(/undefined|NaN/);
		}
	});
});

describe('it uses the estimate when there is one', () => {
	it('it ignores the fixed value —— this is the point of the feature', () => {
		expect(short(step({ minutes: 30 }), est({ seconds: 3 }))).toBe('3s');
	});
	it('sentence says **the count before the time**', () => {
		const t = sentence(step(), est());
		expect(t.indexOf('55')).toBeLessThan(t.indexOf('estimated'));
		expect(t).toContain('chunks');
	});
	it('a step that cannot be counted does not invent a count', () => {
		const t = sentence(step(), est({ units: null, unit: '' }));
		expect(t).not.toContain('Work:');
		expect(t).toContain('estimated');
	});
	it('it asks about a DB overwrite even when short —— because it cannot be undone', () => {
		expect(heavy(step({ writes_db: true }), est({ seconds: 1 }))).toBe(true);
	});
	it('it does not ask when the DB is untouched and it is under a minute', () => {
		expect(heavy(step({ writes_db: false }), est({ seconds: 59 }))).toBe(false);
		expect(heavy(step({ writes_db: false }), est({ seconds: 60 }))).toBe(true);
	});
});
