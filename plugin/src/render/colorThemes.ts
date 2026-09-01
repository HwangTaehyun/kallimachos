/**
 * Colour themes: concrete palettes as sets (G2.5 feedback —— better than shuffling is offering classic combinations).
 * The selection criterion: how they read against a near-black space background.  This is not a commercial project, so brand colours are borrowed freely.
 * How they apply: tinted onto the user's colour groups in order (cycling when there are more groups than colours).
 *
 * ⚠ `name` is shown **to every user regardless of language** —— unlike stylePresets there is no
 *   `nameEn` and no i18n key (ControlPanel.ts:85 picks by language only for presets).  These were
 *   Chinese, so a non-Chinese reader saw six unreadable names in the theme dropdown.
 */
export interface ColorTheme {
	id: string;
	name: string;
	colors: string[];
}

export const COLOR_THEMES: ColorTheme[] = [
	{
		id: 'hubble',
		name: 'Hubble deep space',
		// The Hubble telescope's false-colour palette: ionised-oxygen cyan, hydrogen-α gold, sulphur-ion rust red —— orthodox astrophotography
		colors: ['#46d4dc', '#ffc35c', '#d05a32', '#7fd0a0', '#e8d9a0', '#5a9bd8', '#d87fa8', '#9a7fe0', '#cfd8e8'],
	},
	{
		id: 'tiktok',
		name: 'TikTok neon',
		// TikTok's cyan/red primaries + derived brightness steps —— neon on black
		colors: ['#25f4ee', '#fe2c55', '#ffffff', '#7ae8e2', '#ff7a9c', '#19b8b2', '#c2244a', '#a8f0ec', '#ffd0dc'],
	},
	{
		id: 'sunset',
		name: 'Sunset film',
		// The Instagram gradient: orange → magenta → purple → blue
		colors: ['#f58529', '#dd2a7b', '#8134af', '#515bd4', '#feda77', '#e1306c', '#c13584', '#fd8d32', '#405de6'],
	},
	{
		id: 'cyber',
		name: 'Cyber city',
		// Cyberpunk 2077: signal yellow / electric cyan / warning red
		colors: ['#fcee0a', '#00f0ff', '#ff003c', '#9d00ff', '#00ff9f', '#ff6ec7', '#3df5ff', '#ffe600', '#c800ff'],
	},
	{
		id: 'matrix',
		name: 'The Matrix',
		// Matrix pure green steps —— monochrome is taste too
		colors: ['#00ff41', '#33ff66', '#00cc34', '#66ff8c', '#00b32d', '#80ffa0', '#1aff4d', '#00e639', '#4dff79'],
	},
	{
		id: 'aurora',
		name: 'Aurora',
		// Spotify green + ice blue + violet —— a high-latitude night sky
		colors: ['#1db954', '#00d4ff', '#7f5fff', '#38f0c0', '#4fa8ff', '#9f7fff', '#22e6a8', '#66c2ff', '#b08fff'],
	},
];
