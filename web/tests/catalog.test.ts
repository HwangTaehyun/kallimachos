import { describe, it, expect } from 'vitest';
import { localizeCatalog, STEPS_KO, GROUPS_KO } from '../src/i18n/catalog';

//  Catalogue localisation.  The single source is src/status.py (English) and this is the Korean override.
//  2026-09-01: it was built the other way round at first (Korean in py, English in web) and flipped, because the CLI has to be English too.
const STEP_IDS = ['distill', 'promote', 'extract', 'index', 'export', 'refresh_kg', 'apply_aliases', 'rebuild_all', 'sync', 'verify'];
const GROUP_IDS = ['main', 'combo', 'partial', 'check'];
const step = (id: string, title: string, desc: string) => ({ id, title, desc, group: 'main', order: 1, minutes: 1, needs_llm: false, writes_db: false }) as never;
const group = (id: string, title: string, desc: string) => ({ id, title, desc }) as never;

describe('localizeCatalog', () => {
	it('the Korean dictionary covers status.py’s id set exactly (a gap leaks English onto the Korean screen)', () => {
		expect(Object.keys(STEPS_KO).sort()).toEqual([...STEP_IDS].sort());
		expect(Object.keys(GROUPS_KO).sort()).toEqual([...GROUP_IDS].sort());
	});

	it('en: the English API text is left alone (English exists once, in py)', () => {
		const r = { steps: [step('distill', 'Distill sessions', 'Logs → docs.')], groups: [group('main', 'Full pipeline', 'Order matters.')] };
		expect(localizeCatalog(r, 'en')).toEqual(r);
	});


	it('ko: it overrides by id, and an unknown id falls back to the API source text', () => {
		const r = { steps: [step('distill', 'Distill sessions', '…'), step('brand_new', 'Brand new', 'Not in the dict')], groups: [group('check', 'Checks', '…')] };
		const l = localizeCatalog(r, 'ko');
		expect(l.steps[0]).toMatchObject({ title: '세션 정제' });
		expect(l.steps[1]).toMatchObject({ title: 'Brand new', desc: 'Not in the dict' });
		expect(l.groups[0]).toMatchObject({ title: '검사' });
	});

	it('ko: index’s chunk size follows the number in the API text (it changes when the setting does)', () => {
		const r = { steps: [step('index', 'Rebuild knowledge DB', 'Chunk the vault into 1,200 chars, embed…')], groups: [] };
		expect(localizeCatalog(r, 'ko').steps[0].desc).toContain('1,200자');
		const ko = { steps: [step('index', 'Rebuild the knowledge DB', 'vault 를 800자로 잘라…')], groups: [] };
		expect(localizeCatalog(ko, 'ko').steps[0].desc).toContain('800자');
	});
});
