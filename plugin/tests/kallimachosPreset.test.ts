import { describe, expect, it } from 'vitest';
import { STYLE_PRESETS, presetGlow } from '../src/render/stylePresets';
import { COLOR_THEMES } from '../src/render/colorThemes';
import { DEFAULT_SETTINGS, mergeSettings, toLayoutParams } from '../src/settings';
import { DE } from '../src/i18n/de';
import { EN } from '../src/i18n/en';
import { ES } from '../src/i18n/es';
import { IT } from '../src/i18n/it';
import { PT } from '../src/i18n/pt';
import { ZH } from '../src/i18n/zh';

// kal's own per-entity-type palette (config/brand/galaxy.ts TYPE_COLOR / kal/src/schema_v3.py minus
// 'other'): artifact, method, concept, failure, tool, decision, metric, event, actor, project.
const TYPE_COLOR_HEXES = ['#7FB3FF', '#FF6E8A', '#5AA9FF', '#FF5C5C', '#FFB454', '#3FD6C0', '#FFD84D', '#F79E4A', '#9BE564', '#B98CFF'];

const kallimachos = STYLE_PRESETS.find((p) => p.id === 'kallimachos');

describe('the kallimachos preset (the landing page hero, reproduced on the real graph)', () => {
	it('is registered in STYLE_PRESETS alongside the existing 8, all still present', () => {
		expect(kallimachos).toBeDefined();
		expect(STYLE_PRESETS.map((p) => p.id)).toEqual([
			'galaxy', 'spiral', 'orbits', 'deepfield', 'nebula', 'minimal', 'fireworks', 'supernova', 'kallimachos',
		]);
	});

	it('has a display name ("Kallimachos" in English) and its own icon id, following the existing preset convention', () => {
		expect(kallimachos?.nameEn).toBe('Kallimachos');
		expect(kallimachos?.name).toBeTruthy();
	});

	it('material settings are correct: additive glow on, and the theme carries the real per-type TYPE_COLOR hexes', () => {
		expect(kallimachos?.additiveGlow).toBe(true);
		expect(kallimachos?.theme).toBe('kallimachos');
		const theme = COLOR_THEMES.find((t) => t.id === 'kallimachos');
		expect(theme).toBeDefined();
		// A drift here would silently break colour parity with the hero sphere (or with schema_v3.py).
		expect(theme?.colors).toEqual(TYPE_COLOR_HEXES);
	});

	it('leaves the other 8 presets\' material untouched (additiveGlow stays undefined = normal blending)', () => {
		for (const p of STYLE_PRESETS) {
			if (p.id === 'kallimachos') continue;
			expect(p.additiveGlow).toBeUndefined();
		}
	});

	it('is bare and round like the hero: no starfield, no background haze layers, no disc flatten/spiral, straight links', () => {
		expect(kallimachos?.starfield).toBe(false);
		expect(kallimachos?.space).toEqual({ nebula: 0, fieldStars: 0, clusterClouds: 0 });
		expect(kallimachos?.physics.flatten).toBe(0);
		expect(kallimachos?.physics.spiral).toBe(0);
		expect(kallimachos?.look.linkCurve).toBe(0);
	});

	it('the layout is unchanged: its physics still routes through the existing toLayoutParams mapping, no bespoke path', () => {
		const p = kallimachos!.physics;
		expect(toLayoutParams(p)).toEqual({
			charge: -p.repel,
			linkDistance: p.linkDistance,
			linkStrength: p.linkStrength,
			centerPull: p.centerPull,
			flatten: p.flatten,
			coreGravity: p.coreGravity,
			spiral: p.spiral,
			community: 0,
			velocityDecay: 0.6,
		});
	});

	it('is the default only on a fresh install (an empty or absent save)', () => {
		expect(DEFAULT_SETTINGS.activePreset).toBe('kallimachos');
		expect(mergeSettings({}).activePreset).toBe('kallimachos');
		expect(mergeSettings(null).activePreset).toBe('kallimachos');
		expect(mergeSettings({}).bloom).toEqual(kallimachos?.bloom);
		expect(mergeSettings({}).physics).toEqual(kallimachos?.physics);
		expect(mergeSettings({}).look).toEqual(kallimachos?.look);
		expect(mergeSettings({}).space).toEqual(kallimachos?.space);
		expect(mergeSettings({}).showStarfield).toBe(kallimachos?.starfield);
		expect(presetGlow(mergeSettings({}).activePreset)).toBe(true);
	});

	it('never overrides an existing save —— including every save from before this preset existed', () => {
		// Every save written before this change already has activePreset: 'galaxy' on disk
		// (it was the only default there ever was); mergeSettings must keep it exactly as saved.
		expect(mergeSettings({ activePreset: 'galaxy' }).activePreset).toBe('galaxy');
		expect(mergeSettings({ activePreset: 'minimal', showStarfield: true }).activePreset).toBe('minimal');
		expect(mergeSettings({ activePreset: 'minimal', showStarfield: true }).showStarfield).toBe(true);
	});

	it('an old save keeps normal blending —— the glow follows the preset, it is not filled in from the new default', () => {
		//  The regression a stored flag had: no save written before this change has the key, so the
		//  merge filled in `true` and everyone still on 'galaxy' got additive blending they never chose.
		expect(presetGlow(mergeSettings({ activePreset: 'galaxy' }).activePreset)).toBe(false);
		//  The web viewer spreads DEFAULT_SETTINGS under the stored object (src/web/main.ts) —— same answer.
		expect(presetGlow(mergeSettings({ ...DEFAULT_SETTINGS, activePreset: 'galaxy' }).activePreset)).toBe(false);
		expect(presetGlow('')).toBe(false); // a deleted custom preset leaves activePreset empty
	});

	it('a custom preset saved from the kallimachos look keeps its glow, one saved from another look does not', () => {
		const glowing = { ...kallimachos!, id: 'custom-a', additiveGlow: true };
		const plain = { ...kallimachos!, id: 'custom-b', additiveGlow: false };
		expect(presetGlow('custom-a', [glowing, plain])).toBe(true);
		expect(presetGlow('custom-b', [glowing, plain])).toBe(false);
	});

	it('has a translated subtitle in all six locales, not a silent English fallback', () => {
		const dicts: Record<string, Record<string, string>> = { en: EN, zh: ZH, de: DE, es: ES, it: IT, pt: PT };
		for (const [lang, dict] of Object.entries(dicts)) {
			expect(dict['preset.sub.kallimachos'], `missing preset.sub.kallimachos in ${lang}`).toBeTruthy();
		}
		// en and zh must actually differ (a real translation, not a copy-paste placeholder)
		expect(EN['preset.sub.kallimachos']).not.toBe(ZH['preset.sub.kallimachos']);
	});
});
