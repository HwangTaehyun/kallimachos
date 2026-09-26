import type { BloomSettings, LookSettings, PhysicsSettings, SpaceSettings } from '../settings';

/**
 * A style preset = a complete look: glow + physics + appearance + the starfield switch + the deep-space shape layers + the colour theme + the framing elevation.
 * The v0.3 panel rework: merged into one flat list (told apart by name and icon), with each preset differing along all five of
 * "starfield or not / palette / size / physics / glow".  The names and subtitles go through i18n (preset.sub.<id>),
 * and the icons are drawn by src/overlay/presetIcons.ts from the id.  The numbers are a starting point, finally tuned by eye on a real device.
 *
 * v0.4 adds two dimensions (link curvature look.linkCurve + the background shape layers space), and the value-setting principle (Rick's call) is:
 * fit each preset's own character and pull the presets apart from each other —— galaxy restrained and orthodox / spiral flowing / orbits wide arcs with clean space /
 * deep field straight lines + a sea of floating stars (the tiny distant points filling a Hubble deep-field frame) / nebula full of cloud / minimal everything off /
 * fireworks a burst on pure black / supernova a warm dust afterglow.
 */
export interface StylePreset {
	id: string;
	/** The zh display name.  It stays Chinese on purpose: `ControlPanel.ts:85` picks
	 *  `getLang() === 'zh' ? name : (nameEn ?? name)`, so this is locale data, not a comment. */
	name: string;
	nameEn?: string;
	/** The starfield backdrop switch (the sphere-shell background stars) */
	starfield: boolean;
	/**
	 * Additive point-sprite blending for the node material (default/undefined = the normal alpha
	 * blending every other preset already uses). Deep-space only: AggregateRenderer forces normal
	 * blending back on whenever the daylight visual direction is active, so a preset that turns
	 * this on does not wash out to white the moment someone flips to the adaptive/light theme.
	 * See AggregateRenderer.setNodeBlending.
	 */
	additiveGlow?: boolean;
	/** The deep-space background shape layers (the nebula backdrop / floating field stars / cluster clouds, 0 = off) */
	space: SpaceSettings;
	/** The colour theme id (see colorThemes.ts) —— a preset applies it too */
	theme: string;
	/** The camera's framing elevation (degrees): disc types look down on the arms at ~50°, sphere and cluster types stay at 18° */
	frameElevDeg?: number;
	bloom: BloomSettings;
	physics: PhysicsSettings;
	look: LookSettings;
}

export const STYLE_PRESETS: StylePreset[] = [
	{
		id: 'galaxy', name: '银河', nameEn: 'Galaxy', starfield: true, theme: 'hubble', frameElevDeg: 50,
		space: { nebula: 0.35, fieldStars: 0.25, clusterClouds: 0.3 },
		bloom: { strength: 0.35, radius: 0.35, threshold: 0.22 },
		physics: { repel: 170, linkDistance: 55, linkStrength: 1.1, centerPull: 0.05, flatten: 0.55, coreGravity: 0.1, spiral: 0.02 },
		look: { nodeSize: 1, linkOpacity: 0.14, linkCurve: 0.35, twinkle: 0.5, sizeBy: 'degree' },
	},
	{
		// Pulled apart from "galaxy" (Rick's feedback): an extremely flat disc + the spiral force at maximum + a strong core + strongly curved flowing links + looking further down on the arms
		id: 'spiral', name: '旋臂', nameEn: 'Spiral', starfield: true, theme: 'aurora', frameElevDeg: 62,
		space: { nebula: 0.4, fieldStars: 0.35, clusterClouds: 0.35 },
		bloom: { strength: 0.5, radius: 0.45, threshold: 0.2 },
		physics: { repel: 150, linkDistance: 62, linkStrength: 1.15, centerPull: 0.06, flatten: 0.75, coreGravity: 0.22, spiral: 0.095 },
		look: { nodeSize: 1, linkOpacity: 0.16, linkCurve: 0.72, twinkle: 0.6, sizeBy: 'degree' },
	},
	{
		id: 'orbits', name: '轨道', nameEn: 'Orbits', starfield: true, theme: 'sunset', frameElevDeg: 35,
		space: { nebula: 0.12, fieldStars: 0.1, clusterClouds: 0 },
		bloom: { strength: 0.42, radius: 0.35, threshold: 0.24 },
		physics: { repel: 200, linkDistance: 95, linkStrength: 0.9, centerPull: 0.09, flatten: 0.5, coreGravity: 0.2, spiral: 0 },
		look: { nodeSize: 1.15, linkOpacity: 0.11, linkCurve: 0.75, twinkle: 0.4, sizeBy: 'degree' },
	},
	{
		id: 'deepfield', name: '深空场', nameEn: 'Deep Field', starfield: true, theme: 'hubble', frameElevDeg: 18,
		space: { nebula: 0.55, fieldStars: 0.65, clusterClouds: 0.25 },
		bloom: { strength: 0.28, radius: 0.4, threshold: 0.3 },
		physics: { repel: 300, linkDistance: 110, linkStrength: 1.5, centerPull: 0.02, flatten: 0, coreGravity: 0, spiral: 0 },
		look: { nodeSize: 0.8, linkOpacity: 0.08, linkCurve: 0, twinkle: 0.3, sizeBy: 'degree' },
	},
	{
		id: 'nebula', name: '星云', nameEn: 'Nebula', starfield: false, theme: 'tiktok', frameElevDeg: 18,
		space: { nebula: 0.8, fieldStars: 0.45, clusterClouds: 0.7 },
		bloom: { strength: 0.6, radius: 0.32, threshold: 0.2 },
		physics: { repel: 150, linkDistance: 70, linkStrength: 0.9, centerPull: 0.03, flatten: 0.15, coreGravity: 0.03, spiral: 0 },
		look: { nodeSize: 1.05, linkOpacity: 0.2, linkCurve: 0.45, twinkle: 0.5, sizeBy: 'degree' },
	},
	{
		id: 'minimal', name: '极简', nameEn: 'Minimal', starfield: false, theme: 'matrix', frameElevDeg: 18,
		space: { nebula: 0, fieldStars: 0, clusterClouds: 0 },
		bloom: { strength: 0, radius: 0.3, threshold: 0.3 },
		physics: { repel: 230, linkDistance: 80, linkStrength: 1, centerPull: 0.04, flatten: 0, coreGravity: 0, spiral: 0 },
		look: { nodeSize: 0.8, linkOpacity: 0.07, linkCurve: 0, twinkle: 0, sizeBy: 'degree' },
	},
	{
		id: 'fireworks', name: '烟火', nameEn: 'Fireworks', starfield: false, theme: 'cyber', frameElevDeg: 18,
		space: { nebula: 0, fieldStars: 0, clusterClouds: 0 },
		bloom: { strength: 1, radius: 0.38, threshold: 0.16 },
		physics: { repel: 150, linkDistance: 58, linkStrength: 1.3, centerPull: 0.05, flatten: 0, coreGravity: 0, spiral: 0 },
		look: { nodeSize: 1.2, linkOpacity: 0.28, linkCurve: 0.2, twinkle: 1.3, sizeBy: 'degree' },
	},
	{
		id: 'supernova', name: '超新星', nameEn: 'Supernova', starfield: false, theme: 'sunset', frameElevDeg: 22,
		space: { nebula: 0.45, fieldStars: 0.2, clusterClouds: 0.4 },
		bloom: { strength: 0.9, radius: 0.4, threshold: 0.18 },
		physics: { repel: 260, linkDistance: 62, linkStrength: 1.1, centerPull: 0.03, flatten: 0, coreGravity: -0.08, spiral: 0 },
		look: { nodeSize: 1.3, linkOpacity: 0.24, linkCurve: 0.3, twinkle: 1.5, sizeBy: 'degree' },
	},
	{
		// The landing page's hero sphere (config/brand/galaxy.ts), reproduced on the real entity graph.
		// Bare and true to the data —— no starfield, no nebula/field-stars/cluster-clouds haze (all
		// three space layers off, like minimal), a round cloud rather than a disc (flatten 0, no
		// spiral, like deepfield), and straight faint filaments (linkCurve 0). additiveGlow gives the
		// node sprites the hero's "light adding up" look on the shared node material —— a preset
		// data flag, not a second rendering pipeline (see AggregateRenderer.setNodeBlending). Real
		// per-type colour comes from the data itself in KDB mode (loadKdbGraph → setExplicitColors);
		// the 'kallimachos' theme below only covers the non-KDB fallback so the two modes match.
		// physics matches 'minimal' almost exactly (proven to settle into an even round cloud on a
		// real community-structured graph) —— coreGravity in particular stays at 0: a nonzero pull
		// toward the global centre fights the natural per-community spacing and drags the layout into
		// a hub-and-spoke lump instead of a filled sphere (measured while previewing this preset).
		id: 'kallimachos', name: '卡利马科斯', nameEn: 'Kallimachos', starfield: false, additiveGlow: true, theme: 'kallimachos', frameElevDeg: 18,
		space: { nebula: 0, fieldStars: 0, clusterClouds: 0 },
		bloom: { strength: 0.32, radius: 0.4, threshold: 0.22 },
		physics: { repel: 230, linkDistance: 80, linkStrength: 1, centerPull: 0.045, flatten: 0, coreGravity: 0, spiral: 0 },
		look: { nodeSize: 0.85, linkOpacity: 0.1, linkCurve: 0, twinkle: 0, sizeBy: 'degree' },
	},
];

/**
 * Whether the preset with this id draws its nodes with additive glow. Derived from the preset, never
 * stored in settings: a stored flag was filled with the new default (`true`) for every save written
 * before it existed, so anyone still on 'galaxy' would have lost the normal blending they had
 * (2026-09-26 review). Only applying, saving or deleting a preset changes activePreset —— the
 * sliders do not —— so the active preset is always the right thing to ask.
 */
export const presetGlow = (id: string, custom: readonly StylePreset[] = []): boolean =>
	[...STYLE_PRESETS, ...custom].find((p) => p.id === id)?.additiveGlow ?? false;
