import { boundedTagHubLimit } from './data/tagLens';

export interface BloomSettings {
	strength: number;
	radius: number;
	threshold: number;
}

export interface PhysicsSettings {
	repel: number; // positive; the layout negates it to use as charge
	linkDistance: number;
	linkStrength: number; // a multiplier: 1 = the d3 default (1/min(endpoint degree))
	centerPull: number;
	flatten: number; // 0 = a sphere, >0 = flattened on Y → a galactic disc
	coreGravity: number; // radial core gravity: a dense bright core (degree-weighted); negative = an outward burst (a supernova)
	spiral: number; // the tangential spiral-arm force: 0 = no arms
}

export type SizeBy = 'degree' | 'fileSize' | 'uniform';

export interface LookSettings {
	nodeSize: number; // a multiplier
	linkOpacity: number;
	linkCurve: number; // link curvature 0–1 (0 = straight, the geometry degenerating to one segment, equivalent to the old behaviour)
	twinkle: number; // the bright stars' twinkle rate (0 = off)
	sizeBy: SizeBy; // what a node's "mass" is based on
}

/** The deep-space background shape layers (v0.4): three independent switches (0 = off), presets give combinations, the user can customise each layer */
export interface SpaceSettings {
	nebula: number; // the nebula backdrop's strength 0–1 (the texture is baked once; the strength only adjusts opacity)
	fieldStars: number; // the floating field stars' density 0–1 (scattered through the volume, larger near and smaller far, for parallax)
	clusterClouds: number; // the cluster clouds' strength 0–1 (coloured clouds over dense star clusters)
}

export type VisualPreset = 'deep-space' | 'adaptive';

export interface TourSettings {
	speed: number; // the flight-speed multiplier for wander / connect-two-notes
}

export interface GalaxySettings {
	bloom: BloomSettings;
	physics: PhysicsSettings;
	look: LookSettings;
	space: SpaceSettings;
	cruise: boolean;
	cruiseSpeed: number; // the cruise angular-speed multiplier
	showUnresolved: boolean;
	showOrphans: boolean;
	/** The tag-exploration master switch: read the notes' tags and show chips; it adds no hub nodes by default. */
	showTags: boolean;
	/** The current Tag Lens's tag node id (tag:#...); null = not active */
	tagLens: string | null;
	/** Colour by a note's first tag; has no effect while showTags is off. */
	colorByTag: boolean;
	/** Optional top-N tag hubs; they enter the graph and the layout only with showTags also on. */
	showTagHubs: boolean;
	/** A hard limit of 5–50 on the tag hub count. */
	tagHubLimit: number;
	/** The filter's primary control —— the groups switched off in the legend.  An empty array = show everything.
	    It says 'Folders', but depending on the mode it holds folder, entity-type or community names.
	    Per-mode storage is hiddenByNamespace below. */
	/** The groups switched off in the currently active namespace.  A copy of hiddenByNamespace below. */
	hiddenFolders: string[];
	/**
	 * The per-namespace filter store.
	 *
	 * Saving hiddenFolders alone **contaminates across modes** —— quit in community mode with a
	 * community switched off, open in note mode, and that community name is compared against
	 * folder names.  The result is either a ghost filter hiding nothing, or a folder that happens
	 * to share the name being hidden without a word.  (2026-08-18 adversarial review)
	 */
	hiddenByNamespace: { notes: string[]; type: string[]; community: string[] };
	/** Filter, escape hatch: a text query, for the cross-cutting patterns a legend cannot express (an Index scattered everywhere, say).  The syntax is in data/noteFilter.ts */
	filterQuery: string;
	/** Ghost edges: pending link suggestions from the Constellation companion plugin (dashed; they turn solid automatically once accepted) */
	showGhostEdges: boolean;
	/** The deep-space starfield background switch (a user option) */
	showStarfield: boolean;
	colorTheme: string;
	qualityOverride: 'auto' | 'high' | 'low' | 'mobile'; // the mobile tier on a desktop = a mobile simulation // the most recently applied colour theme id; 'imported' = a 2D import, 'custom' = after a shuffle
	preset: VisualPreset;
	/** The currently active style preset's id (for the panel's "set by X / customized" marker) */
	activePreset: string;
	/** The custom presets the user saved (reorderable, deletable) */
	customPresets: import('./render/stylePresets').StylePreset[];
	/** The interface language: auto = follow Obsidian (see src/i18n); en/zh/de/it/es/pt */
	language: import('./i18n').LangPref;
	/** Each collapsible panel section's expanded state (a stable sectionId → whether it is open) */
	panelSections: Record<string, boolean>;
	/** The floating panel's width (px, adjustable by dragging its right edge) */
	panelWidth: number;
	/** Has the first-run hint been seen (once it has, it no longer opens by default) */
	hintsSeen: boolean;
	/** The relation depth highlighted on selection: 1 = first-degree neighbours, 2 = including the second degree (graded dimming) */
	/** The selection's neighbour-highlight depth.  The KDB entity graph has thousands of nodes, so 2 is not enough. */
	selectionDepth: 1 | 2 | 3 | 4;
	/** Knowledge DB mode — a node is an entity, not a page. */
	kdbMode: boolean;
	/** The depth filter — hide anything more than N hops from the selected node.  0 = off */
	depthFilter: 0 | 1 | 2 | 3 | 4;
	/** The degree floor — leave entities with fewer connections than this out of the graph.  1 = everything */
	kdbMinDegree: number;
	/** Group the entity graph by type or by community */
	kdbGroupBy: 'type' | 'community';
	/** Tour / autopilot (v0.3) */
	tour: TourSettings;
	/** The 2D palette imported once from .obsidian/graph.json (re-importable from the panel) */
	colorGroups: import('./settings/graphJsonImport').ColorGroup[];
	/** The settled-coordinate cache: used for a warm start (id → [x,y,z]) */
	positionCache: Record<string, [number, number, number]>;
	/** The signature of the layout those coordinates were made with.  Different from the current one and the cache is discarded —— see below */
	positionCacheKey: string;
}

// The default = the reworked "galaxy" style preset: a dense bright core + a flattened disc + faint spiral arms (the v0.2 galaxy layout rework)
export const DEFAULT_SETTINGS: GalaxySettings = {
	bloom: { strength: 0.35, radius: 0.35, threshold: 0.22 },
	physics: { repel: 170, linkDistance: 55, linkStrength: 1.1, centerPull: 0.05, flatten: 0.55, coreGravity: 0.1, spiral: 0.02 },
	look: { nodeSize: 1, linkOpacity: 0.14, linkCurve: 0.35, twinkle: 0.5, sizeBy: 'degree' },
	space: { nebula: 0.35, fieldStars: 0.25, clusterClouds: 0.3 },
	cruise: true,
	cruiseSpeed: 1,
	showUnresolved: false,
	showOrphans: true,
	showTags: false,
	tagLens: null,
	colorByTag: false,
	showTagHubs: false,
	tagHubLimit: 20,
	hiddenFolders: [],
	hiddenByNamespace: { notes: [], type: [], community: [] },
	filterQuery: '',
	// Off by default: ghost edges depend on the Constellation companion plugin (not yet formally released), shipped with 0.4.0 but not bothering ordinary users yet
	showGhostEdges: false,
	showStarfield: true,
	colorTheme: 'imported',
	qualityOverride: 'auto',
	preset: 'deep-space',
	activePreset: 'galaxy',
	customPresets: [],
	language: 'auto',
	panelSections: {},
	panelWidth: 300,
	hintsSeen: false,
	selectionDepth: 1,
	kdbMode: false,
	depthFilter: 0,
	kdbMinDegree: 1,
	kdbGroupBy: 'type',
	tour: { speed: 1 },
	colorGroups: [],
	positionCache: {},
	positionCacheKey: '',
};

export function mergeSettings(saved: unknown): GalaxySettings {
	const d = DEFAULT_SETTINGS;
	const s = (saved ?? {}) as Partial<Record<keyof GalaxySettings, Record<string, unknown>>>;
	const sv = (saved ?? {}) as Partial<Record<keyof GalaxySettings, unknown>> & {
		cruise?: unknown;
		showUnresolved?: unknown;
		preset?: unknown;
		colorGroups?: unknown[];
		positionCache?: unknown;
		positionCacheKey?: unknown;
	};
	const num = (v: unknown, fallback: number) => (typeof v === 'number' && isFinite(v) ? v : fallback);
	return {
		bloom: {
			strength: num(s.bloom?.['strength'], d.bloom.strength),
			radius: num(s.bloom?.['radius'], d.bloom.radius),
			threshold: num(s.bloom?.['threshold'], d.bloom.threshold),
		},
		physics: {
			repel: num(s.physics?.['repel'], d.physics.repel),
			linkDistance: num(s.physics?.['linkDistance'], d.physics.linkDistance),
			linkStrength: num(s.physics?.['linkStrength'], d.physics.linkStrength),
			centerPull: num(s.physics?.['centerPull'], d.physics.centerPull),
			flatten: num(s.physics?.['flatten'], d.physics.flatten),
			coreGravity: num(s.physics?.['coreGravity'], d.physics.coreGravity),
			spiral: num(s.physics?.['spiral'], d.physics.spiral),
		},
		look: {
			nodeSize: num(s.look?.['nodeSize'], d.look.nodeSize),
			linkOpacity: num(s.look?.['linkOpacity'], d.look.linkOpacity),
			linkCurve: num(s.look?.['linkCurve'], d.look.linkCurve),
			twinkle: num(s.look?.['twinkle'], d.look.twinkle),
			sizeBy: (['degree', 'fileSize', 'uniform'] as const).includes(s.look?.['sizeBy'] as SizeBy)
				? (s.look?.['sizeBy'] as SizeBy)
				: d.look.sizeBy,
		},
		space: {
			nebula: num(s.space?.['nebula'], d.space.nebula),
			fieldStars: num(s.space?.['fieldStars'], d.space.fieldStars),
			clusterClouds: num(s.space?.['clusterClouds'], d.space.clusterClouds),
		},
		cruise: typeof sv.cruise === 'boolean' ? sv.cruise : d.cruise,
		cruiseSpeed: num((sv as Record<string, unknown>)['cruiseSpeed'], d.cruiseSpeed),
		showUnresolved: typeof sv.showUnresolved === 'boolean' ? sv.showUnresolved : d.showUnresolved,
		showOrphans:
			typeof (sv as Record<string, unknown>)['showOrphans'] === 'boolean'
				? ((sv as Record<string, unknown>)['showOrphans'] as boolean)
				: d.showOrphans,
		showTags:
			typeof (sv as Record<string, unknown>)['showTags'] === 'boolean'
				? ((sv as Record<string, unknown>)['showTags'] as boolean)
				: d.showTags,
		tagLens:
			typeof (sv as Record<string, unknown>)['tagLens'] === 'string'
				? ((sv as Record<string, unknown>)['tagLens'] as string)
				: null,
		colorByTag:
			typeof (sv as Record<string, unknown>)['colorByTag'] === 'boolean'
				? ((sv as Record<string, unknown>)['colorByTag'] as boolean)
				: d.colorByTag,
		showTagHubs:
			typeof (sv as Record<string, unknown>)['showTagHubs'] === 'boolean'
				? ((sv as Record<string, unknown>)['showTagHubs'] as boolean)
				: d.showTagHubs,
		tagHubLimit: boundedTagHubLimit(
			num((sv as Record<string, unknown>)['tagHubLimit'], d.tagHubLimit),
		),
		hiddenFolders: Array.isArray((sv as Record<string, unknown>)['hiddenFolders'])
			? ((sv as Record<string, unknown>)['hiddenFolders'] as unknown[]).filter((x): x is string => typeof x === 'string')
			: d.hiddenFolders,
		hiddenByNamespace: (() => {
			const raw = (sv as Record<string, unknown>)['hiddenByNamespace'];
			const pick = (k: string): string[] => {
				const v = (raw as Record<string, unknown> | undefined)?.[k];
				return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
			};
			return raw && typeof raw === 'object'
				? { notes: pick('notes'), type: pick('type'), community: pick('community') }
				: d.hiddenByNamespace;
		})(),
		filterQuery:
			typeof (sv as Record<string, unknown>)['filterQuery'] === 'string'
				? ((sv as Record<string, unknown>)['filterQuery'] as string)
				: d.filterQuery,
		showGhostEdges:
			typeof (sv as Record<string, unknown>)['showGhostEdges'] === 'boolean'
				? ((sv as Record<string, unknown>)['showGhostEdges'] as boolean)
				: d.showGhostEdges,
		showStarfield: typeof sv.showStarfield === 'boolean' ? sv.showStarfield : d.showStarfield,
		colorTheme:
			typeof (sv as Record<string, unknown>)['colorTheme'] === 'string'
				? ((sv as Record<string, unknown>)['colorTheme'] as string)
				: d.colorTheme,
		qualityOverride: (['auto', 'high', 'low', 'mobile'] as const).includes(
			(sv as Record<string, unknown>)['qualityOverride'] as 'auto',
		)
			? ((sv as Record<string, unknown>)['qualityOverride'] as 'auto' | 'high' | 'low' | 'mobile')
			: d.qualityOverride,
		preset: sv.preset === 'adaptive' ? 'adaptive' : 'deep-space',
		activePreset: typeof sv.activePreset === 'string' ? sv.activePreset : d.activePreset,
		customPresets: Array.isArray(sv.customPresets)
			? (sv.customPresets as unknown[])
					.filter(
						(p): p is import('./render/stylePresets').StylePreset =>
							!!p && typeof (p as { id?: unknown }).id === 'string' && typeof (p as { physics?: unknown }).physics === 'object' && typeof (p as { bloom?: unknown }).bloom === 'object' && typeof (p as { look?: unknown }).look === 'object',
					)
					// Presets saved before v0.4 have no linkCurve/space: filled with 0 = keeping the straight-line, no-background look of when they were saved
					.map((p) => ({
						...p,
						look: { ...p.look, linkCurve: num((p.look as { linkCurve?: unknown }).linkCurve, 0) },
						space: {
							nebula: num((p.space as { nebula?: unknown } | undefined)?.nebula, 0),
							fieldStars: num((p.space as { fieldStars?: unknown } | undefined)?.fieldStars, 0),
							clusterClouds: num((p.space as { clusterClouds?: unknown } | undefined)?.clusterClouds, 0),
						},
					}))
			: [],
		language: (['auto', 'en', 'zh', 'de', 'it', 'es', 'pt'] as const).includes(sv.language as 'auto')
			? (sv.language as import('./i18n').LangPref)
			: d.language,
		panelSections:
			sv.panelSections && typeof sv.panelSections === 'object' && !Array.isArray(sv.panelSections)
				? (Object.fromEntries(
						Object.entries(sv.panelSections as Record<string, unknown>).filter(([, v]) => typeof v === 'boolean'),
					) as Record<string, boolean>)
				: {},
		panelWidth: Math.min(Math.max(num(sv.panelWidth, d.panelWidth), 240), 480),
		hintsSeen: typeof sv.hintsSeen === 'boolean' ? sv.hintsSeen : d.hintsSeen,
		selectionDepth: ([1, 2, 3, 4] as const).find((d) => d === sv.selectionDepth) ?? 1,
		kdbMode: sv.kdbMode === true,
		kdbMinDegree: Math.max(1, Math.min(50, Number(sv.kdbMinDegree) || 1)),
		kdbGroupBy: sv.kdbGroupBy === 'community' ? 'community' : 'type',
		depthFilter: ([0, 1, 2, 3, 4] as const).find((d) => d === sv.depthFilter) ?? 0,
		tour: {
			speed: num((sv.tour as Record<string, unknown> | undefined)?.['speed'], d.tour.speed),
		},
		colorGroups: Array.isArray(sv.colorGroups)
			? sv.colorGroups.filter(
					(g): g is import('./settings/graphJsonImport').ColorGroup =>
						typeof (g as { query?: unknown })?.query === 'string' &&
						typeof (g as { color?: unknown })?.color === 'string',
				)
			: [],
		positionCache:
			sv.positionCache && typeof sv.positionCache === 'object'
				? (sv.positionCache as Record<string, [number, number, number]>)
				: {},
		positionCacheKey: typeof sv.positionCacheKey === 'string' ? sv.positionCacheKey : '',
	};
}

/** How the view gets its settings and persistence (avoiding a circular dependency with main.ts) */
export interface SettingsHost {
	settings: GalaxySettings;
	saveSettings(): Promise<void>;
}

export function toLayoutParams(p: PhysicsSettings): import('./types').LayoutParams {
	return {
		charge: -p.repel,
		linkDistance: p.linkDistance,
		linkStrength: p.linkStrength,
		centerPull: p.centerPull,
		flatten: p.flatten,
		coreGravity: p.coreGravity,
		spiral: p.spiral,
		// Community cohesion is meaningful only in KDB mode — GraphController turns it on
		community: 0,
		velocityDecay: 0.6,
	};
}
