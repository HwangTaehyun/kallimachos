export const VIEW_TYPE_GALAXY = 'galaxy-view';
// The defaults and persistence for physics, glow and appearance are in src/settings.ts

// Node size (world units): 2.2×(1+0.5√degree), capped at 6× —— a hub must not swallow the screen
export const NODE_BASE_RADIUS = 2.2;
export const NODE_MAX_RADIUS = NODE_BASE_RADIUS * 6;

// The NASA recipe (the initial bloom values are overwritten immediately by settings, see GraphController.applySettings)
export const BACKGROUND_COLOR = 0x000003;
export const BLOOM_DEFAULTS = { strength: 0.6, radius: 0.4, threshold: 0.18 };
export const LINK_OPACITY = 0.16;

// Camera choreography (the numbers come from the visual spec; the implementer needs no taste)
export const CRUISE = {
	angularSpeed: 0.022, // rad/s
	elevationDeg: 8,
	elevationPeriodS: 90,
	radiusBreath: 0.04,
	radiusPeriodS: 60,
	resumeDelayMs: 6_000, // how long after a real drag or zoom before orbiting resumes (10s was too long; with "only a drag interrupts" it feels more continuous)
	rampUpMs: 2_000,
};
export const FLY_TO = {
	distancePerRadius: 12,
	minDistance: 40,
	maxDistance: 140,
	azimuthOffsetRad: (15 * Math.PI) / 180,
	minMs: 800,
	maxMs: 1800,
	msPerWorldUnit: 0.45,
};

export const STARFIELD_ROTATION_RAD_PER_S = 0.0008;
