/**
 * The quality tiers (M4): three static presets.
 * Platform.isMobile is a hard ceiling (mobile never moves up automatically); a manual override wins outright;
 * auto = starting at high + an FPS watchdog that only steps down (sampled after settling, never coming back up within a session).
 */
export type TierId = 'high' | 'low' | 'mobile';

export interface QualityTier {
	id: TierId;
	pixelRatioCap: number;
	bloomAllowed: boolean; // mobile turns bloom off: the shader's hot core keeps 80% of the look while saving all the post-processing cost
	starScale: number; // starfield point-count scaling (shared by the backdrop stars and the floating field stars)
	linkSegments: number; // the polyline segments per curved link (in effect at curvature >0; always 1 segment at curvature 0)
	clusterCloudsAllowed: boolean; // cluster clouds (large additive sprites carry a fill-rate risk, so mobile turns them off)
	nodeCap: number | null; // take the top N sorted by degree (measured: top 1500 keeps 94% of the link quality)
	linkCap: number | null; // take the top N by min(endpoint degree)
	hubLabels: number;
	neighborLabels: number;
	hoverThrottleMs: number | null; // null = tap only (a touchscreen has no hover)
}

/** The effective device pixel ratio shared by the main WebGL render and the post-processing chain within a tier. */
export function effectivePixelRatio(devicePixelRatio: number, cap: number): number {
	return Math.min(devicePixelRatio, cap);
}

export const TIERS: Record<TierId, QualityTier> = {
	high: {
		id: 'high',
		pixelRatioCap: 2,
		bloomAllowed: true,
		starScale: 1,
		linkSegments: 8,
		clusterCloudsAllowed: true,
		nodeCap: null,
		linkCap: null,
		hubLabels: 14,
		neighborLabels: 20,
		hoverThrottleMs: 30,
	},
	low: {
		id: 'low',
		pixelRatioCap: 1,
		bloomAllowed: true,
		starScale: 0.4,
		linkSegments: 6,
		clusterCloudsAllowed: true,
		nodeCap: null,
		linkCap: null,
		hubLabels: 8,
		neighborLabels: 12,
		hoverThrottleMs: 80,
	},
	mobile: {
		id: 'mobile',
		pixelRatioCap: 1.5,
		bloomAllowed: false,
		starScale: 0.32,
		linkSegments: 4,
		clusterCloudsAllowed: false,
		nodeCap: 1500,
		linkCap: 12_000,
		hubLabels: 6,
		neighborLabels: 8,
		hoverThrottleMs: null,
	},
};
