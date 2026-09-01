// Every token of the two visual directions is collected here (per Rick's protocol: run it and choose by eye, with the G2 gate fixing the defaults)

export interface VisualTokens {
	id: 'deep-space' | 'daylight';
	background: number;
	starfield: boolean;
	space: boolean; // the master switch for the deep-space background shape layers (nebula backdrop / field stars / cluster clouds); forced off in daylight
	motes: boolean; // daylight's dust motes (replacing the starfield)
	bloomEnabled: boolean; // glow on a light ground = haze, so daylight forces it off
	lightMode: boolean; // the node shader variant: an ink disc + a rim
	/** Daylight redirects the 9 hues to paper contrast (keeping the hue, lowering the brightness) */
	nodeLightness: number | null;
	linkInk: string | null; // daylight links = pencil lines (a uniform ink colour, not the endpoint blend)
	linkOpacityScale: number;
	panelClass: string; // the panel's style class
}

export const DEEP_SPACE: VisualTokens = {
	id: 'deep-space',
	background: 0x000003,
	starfield: true,
	space: true,
	motes: false,
	bloomEnabled: true,
	lightMode: false,
	nodeLightness: null,
	linkInk: null,
	linkOpacityScale: 1,
	panelClass: 'gx-theme-dark',
};

export const DAYLIGHT: VisualTokens = {
	id: 'daylight',
	background: 0xf6f4ef, // a warm paper ground
	starfield: false,
	space: false,
	motes: true,
	bloomEnabled: false,
	lightMode: true,
	nodeLightness: 0.44,
	linkInk: '#2e2a24',
	linkOpacityScale: 0.65,
	panelClass: 'gx-theme-light',
};
