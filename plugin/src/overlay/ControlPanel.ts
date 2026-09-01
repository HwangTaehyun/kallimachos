import { Menu, Platform } from 'obsidian';
import type { GalaxySettings } from '../settings';
import { DEFAULT_SETTINGS } from '../settings';
import type { StylePreset } from '../render/stylePresets';
import { STYLE_PRESETS } from '../render/stylePresets';
import type { ColorTheme } from '../render/colorThemes';
import { COLOR_THEMES } from '../render/colorThemes';
import { getLang, t, LANGS } from '../i18n';
import type { Lang, LangPref } from '../i18n';
import { drawPresetIcon } from './presetIcons';
import { Slider } from './Slider';
import type { TopTag } from '../data/tagLens';

export interface ControlPanelCallbacks {
	onBloom: () => void;
	onPhysics: () => void;
	onLook: () => void;
	onSpace: () => void;
	onCruise: (on: boolean) => void;
	onCruiseSpeed: () => void;
	onStylePreset: (p: StylePreset) => void;
	onPresetHover: (p: StylePreset) => void;
	onPresetHoverEnd: () => void;
	onSavePreset: () => void;
	onMovePreset: (i: number, dir: -1 | 1) => void;
	onDeletePreset: (i: number) => void;
	onRenamePreset: (i: number, name: string) => void;
	onRestoreSection: (group: 'bloom' | 'physics' | 'look' | 'space') => void;
	onShowUnresolved: (on: boolean) => void;
	onImportColors: () => void;
	onShuffleColors: () => void;
	onColorTheme: (t: ColorTheme) => void;
	onStarfield: (on: boolean) => void;
	onRecenter: () => void;
	/** Switch between the Knowledge DB entity graph and the note-link graph */
	onKdbMode: (on: boolean) => void;
	/** Change the degree floor (meaningful in entity mode only) */
	onKdbMinDegree: (v: number) => void;
	onKdbGroupBy: (v: 'type' | 'community') => void;
	/** When the mouse enters a legend row.  null = it left.  Lights up that group alone on the graph */
	onGroupHover: (group: string | null) => void;
	/** When the filter changed and needs saving.  Stores it in the per-namespace box. */
	onFilterSaved?: () => void;
	/** The group's description (summary · representative entities).  null when there is none */
	groupInfo: (group: string) => { summary: string; members: string } | null;
	onReveal: () => void;
	onShowOrphans: (on: boolean) => void;
	onShowTags: (on: boolean) => void;
	onTagLens: (tagId: string) => void;
	getTopTags: () => TopTag[];
	onTagColorMode: () => void;
	onTagHubs: () => void;
	onTagHubLimit: () => void;
	onResetTags: () => void;
	onSizeBy: () => void;
	onQuality: () => void;
	onSearch: () => void;
	onTourToggle: () => void;
	onConnectTwo: () => void;
	onTourSpeed: () => void;
	onSectionToggle: (id: string, open: boolean) => void;
	/** Filter, primary: a folder legend click (one click rebuilds; no debounce needed) */
	onHiddenFolders: (hidden: string[]) => void;
	/** The legend data: top-level folders + note counts, descending by count (computed over everything, unaffected by the filter) */
	getFolders: () => { folder: string; count: number }[];
	/** A top-level folder's **actually effective** colour on the graph (including user-imported colorGroups) —— a legend that does not match the graph is decoration */
	folderColorHex: (folder: string) => string;
	/** Filter, escape hatch: a text query; the caller debounces —— every rebuild reruns the layout, so it must not fire per key */
	onFilter: (query: string) => void;
	onLanguage: (pref: LangPref) => void;
	onPanelWidth: (w: number) => void;
	onReset: () => void;
	runScenario: (s: 'S1' | 'S2' | 'S3') => void;
}

const SEC = { look: 'look', space: 'space', physics: 'physics', bloom: 'bloom' } as const;
const SECTION_DEFS: { id: string; group: 'look' | 'physics' | 'bloom' | 'space'; key: Parameters<typeof t>[0] }[] = [
	{ id: SEC.look, group: 'look', key: 'panel.sec.look' },
	{ id: SEC.space, group: 'space', key: 'panel.sec.space' },
	{ id: SEC.physics, group: 'physics', key: 'panel.sec.physics' },
	{ id: SEC.bloom, group: 'bloom', key: 'panel.sec.bloom' },
];

function presetName(p: StylePreset): string {
	return getLang() === 'zh' ? p.name : (p.nameEn ?? p.name);
}
function themeColor(id: string): string {
	return COLOR_THEMES.find((th) => th.id === id)?.colors[0] ?? '#9aa6c0';
}

/**
 * Control panel v4 (the v0.3 panel rework): sectioned by intent (appearance / navigation and motion / the bottom bar); presets are a flat list with icons and subtitles,
 * previewed on hover, committed on click, with a per-section "set by X / customized / reset"; custom presets can be reordered and deleted (confirmed in place).
 */
export class ControlPanel {
	readonly statsEl: HTMLElement;
	readonly advStatsEl: HTMLElement;
	private root: HTMLElement;
	private sliders: Slider[] = [];
	private cruiseBtn: HTMLButtonElement | null = null;
	private unresolvedBtn: HTMLButtonElement | null = null;
	private orphanBtn: HTMLButtonElement | null = null;
	private tagBtn: HTMLButtonElement | null = null;
	private tagHost: HTMLElement | null = null;
	private tagOptionsHost: HTMLElement | null = null;
	private tagColorBtn: HTMLButtonElement | null = null;
	private tagHubsBtn: HTMLButtonElement | null = null;
	private tagHubSliderHost: HTMLElement | null = null;
	private sizeByBtn: HTMLButtonElement | null = null;
	private shuffleBtn: HTMLButtonElement | null = null;
	private starfieldBtn: HTMLButtonElement | null = null;
	private tourPlayBtn: HTMLButtonElement | null = null;
	private presetHost: HTMLElement | null = null;
	private secBadges: Record<string, { badge: HTMLElement; restore: HTMLElement }> = {};
	private filterInput: HTMLInputElement | null = null;
	private filterNoneEl: HTMLElement | null = null;
	private filterAllBtn: HTMLElement | null = null;
	private folderHost: HTMLElement | null = null;
	/** One line saying what the FILTER list is currently filtering (folders ↔ entity types/communities) */
	private filterHintEl: HTMLElement | null = null;
	/** The by-type/by-community segment.  Hidden in note mode —— paint() builds it before the
	    FILTER section, so a local variable cannot hold it. */
	private grpRowEl: HTMLElement | null = null;
	/** The community summary box shown on a legend hover */
	private groupTipEl: HTMLElement | null = null;
	/** The default content to return to when the hover is released (the overall summary) */
	private groupTipIdle: (() => void) | null = null;
	/** Re-aligns the segment highlight with the current settings (so a programmatic switch shows too) */
	private repaintGrp: (() => void) | null = null;
	private helpEl: HTMLElement | null = null;
	private cb: ControlPanelCallbacks;

	constructor(
		parent: HTMLElement,
		private settings: GalaxySettings,
		cb: ControlPanelCallbacks,
	) {
		this.cb = cb;
		this.root = parent.createDiv({ cls: 'galaxy-panel gx-theme-dark' });
		this.root.style.width = `${settings.panelWidth}px`;
		this.buildResizer(cb);

		// —— the top bar ——
		const header = this.root.createDiv({ cls: 'galaxy-panel-header' });
		this.statsEl = header.createDiv({ cls: 'galaxy-panel-stats', text: '…' });
		header.createDiv({ cls: 'gx-head-spacer' });
		// Language: the header shows the current language code, and clicking opens a native Menu with auto + six languages (the current one ticked) —— cleaner than extending a toggle row to many languages
		const langBtn = header.createEl('button', { cls: 'gx-lang-btn', text: this.langLabel(getLang()) });
		langBtn.setAttribute('aria-label', t('set.language'));
		// `aria-haspopup` announces "a menu will open" **before** it is pressed, so it is set
		// statically here.  The Menu shim only learns the invoker when it opens (evt.currentTarget),
		// so attaching it there is already too late on the first encounter.  The shim handles only
		// the dynamic `aria-expanded`.
		langBtn.setAttribute('aria-haspopup', 'menu');
		// Set to 'false' from the start —— without it, there is no announcement that it is even
		// collapsible.  helpBtn in this file works the same way.  Later updates are the Menu shim's job.
		langBtn.setAttribute('aria-expanded', 'false');
		langBtn.addEventListener('click', (e) => this.openLangMenu(e, cb));
		const helpBtn = header.createEl('button', { cls: 'gx-ico', text: '?' });
		// An icon-only button has **a single glyph as its accessible name** —— a screen reader
		// reads it as "question mark button" and nothing else.  It is a toggle, so the open state is exposed too.
		helpBtn.setAttribute('aria-label', t('panel.help'));
		helpBtn.setAttribute('aria-expanded', 'false');
		const collapseBtn = header.createEl('button', { cls: 'gx-ico galaxy-panel-collapse', text: '−' });
		collapseBtn.setAttribute('aria-label', t('panel.collapse'));
		collapseBtn.setAttribute('aria-expanded', 'true');
		const body = this.root.createDiv({ cls: 'galaxy-panel-body' });
		if (Platform.isMobile) {
			body.addClass('is-hidden');
			collapseBtn.setText('+');
		}
		if (Platform.isMobile) collapseBtn.setAttribute('aria-expanded', 'false');
		collapseBtn.addEventListener('click', () => {
			const hidden = body.hasClass('is-hidden');
			body.toggleClass('is-hidden', !hidden);
			collapseBtn.setText(hidden ? '−' : '+');
			collapseBtn.setAttribute('aria-expanded', String(hidden));
		});
		helpBtn.addEventListener('click', () => {
			this.toggleHelp(header);
			helpBtn.setAttribute('aria-expanded',
				String(!!header.querySelector('.gx-help-pop')));
		});

		const s = this.settings;
		const d = DEFAULT_SETTINGS;

		// —— frequently used ——
		const row1 = body.createDiv({ cls: 'galaxy-panel-row' });
		row1.createEl('button', { text: t('panel.search') }).addEventListener('click', cb.onSearch);
		row1.createEl('button', { text: t('panel.recenter') }).addEventListener('click', cb.onRecenter);

		// —— the data source ——
		// Buried in the settings page it would never be found.  It is a switch that changes the whole
		// character of the graph, so it sits at the top of the panel, right under Search/Recenter.
		// A standalone browser build has no vault — switching to note mode there gives an empty graph.
		// A button that can be pressed and does nothing is worse than no button.
		const srcRow = body.createDiv({ cls: 'gx-kdb-row' });
		if (__GALAXY_WEB__) srcRow.addClass('is-hidden');
		const srcBtn = srcRow.createEl('button', { cls: 'gx-kdb-toggle' });
		const paint = () => {
			// '● entity graph' alone is ambiguous between the current state and what pressing it does.
			// It states both what is being viewed and what pressing it gives.
			srcBtn.setText(s.kdbMode ? 'Entity graph  →  to notes' : 'Note link graph  →  to entities');
			srcBtn.toggleClass('is-on', s.kdbMode);
			degRow.toggleClass('is-hidden', !s.kdbMode);
			if (this.filterHintEl) {
				this.filterHintEl.setText(
					!s.kdbMode ? 'Toggle top-level folders on and off'
						: s.kdbGroupBy === 'community'
							? 'Toggle topics on and off (named by top 3 entities)'
							: 'Toggle entity types on and off',
				);
			}
			this.grpRowEl?.toggleClass('is-hidden', !s.kdbMode);
			this.repaintGrp?.();   // so the segment points at reality after a note↔entity switch too
		};
		srcBtn.addEventListener('click', () => {
			s.kdbMode = !s.kdbMode;
			paint();
			cb.onKdbMode(s.kdbMode);
		});
		const degRow = body.createDiv({ cls: 'gx-kdb-row gx-kdb-deg' });
		degRow.createSpan({ cls: 'gx-kdb-deg-l', text: 'Min links' });
		const degIn = degRow.createEl('input', { type: 'range' });
		degIn.min = '1';
		degIn.max = '40';
		degIn.value = String(s.kdbMinDegree);
		const degV = degRow.createSpan({ cls: 'gx-kdb-deg-v', text: `${s.kdbMinDegree}+` });
		degIn.addEventListener('input', () => {
			s.kdbMinDegree = Number(degIn.value);
			degV.setText(`${s.kdbMinDegree}+`);
		});
		// Reload on change (on release) only —— re-reading the JSON every frame during a drag stutters
		degIn.addEventListener('change', () => cb.onKdbMinDegree(s.kdbMinDegree));
		paint();

		// —— the appearance section ——
		body.createDiv({ cls: 'gx-zone-h', text: t('zone.look') });
		this.presetHost = body.createDiv({ cls: 'gx-preset-host' });
		this.buildPresets();

		const section = (id: string, key: Parameters<typeof t>[0], withMarker: 'look' | 'physics' | 'bloom' | 'space' | null) => {
			const det = body.createEl('details', { cls: 'gx-section' });
			if (s.panelSections[id]) det.setAttribute('open', '');
			const sum = det.createEl('summary');
			sum.createSpan({ cls: 'gx-caret', text: '▸' });
			sum.createSpan({ text: t(key) });
			const secBody = det.createDiv({ cls: 'gx-section-body' });
			if (withMarker) {
				sum.createSpan({ cls: 'gx-sec-head-spacer' });
				const badge = sum.createSpan({ cls: 'gx-sec-badge' });
				// The reset has to be **inside the summary** —— because the badge ("customized") is
				// here.  It was moved into the body once and moved back: sections are collapsed by
				// default, so only the badge showed and the button undoing it appeared only when
				// expanded.  The words "this changed" were visible while the way to undo it was not.
				//
				// A <button> inside a summary is valid.  All that is needed is stopping the click
				// from toggling the details —— Enter/Space also arrive as a click on a button, so one place suffices.
				const restore = sum.createEl('button', { cls: 'gx-sec-restore', text: t('sec.restore') });
				restore.addEventListener('click', (e) => {
					e.preventDefault();
					e.stopPropagation();
					cb.onRestoreSection(withMarker);
				});
				this.secBadges[id] = { badge, restore };
			}
			det.addEventListener('toggle', () => cb.onSectionToggle(id, det.open));
			return secBody;
		};

		// —— the filter (#11) ——
		// The controls for "which notes enter the graph" live here; tag exploration has its own separate section.
		this.buildFilterSection(body, cb);
		this.buildTagsSection(body, cb);

		// Appearance and palette
		const lookSec = section(SEC.look, 'panel.sec.look', 'look');
		this.sliders.push(
			new Slider(lookSec, { label: t('slider.look.nodeSize'), min: 0.3, max: 2.5, step: 0.05, defaultValue: d.look.nodeSize, get: () => s.look.nodeSize, set: (v) => (s.look.nodeSize = v), fmt: (v) => `${v.toFixed(2)}×`, onInput: () => this.tracked(cb.onLook) }),
			new Slider(lookSec, { label: t('slider.look.linkOpacity'), min: 0, max: 0.6, step: 0.01, defaultValue: d.look.linkOpacity, get: () => s.look.linkOpacity, set: (v) => (s.look.linkOpacity = v), onInput: () => this.tracked(cb.onLook) }),
			new Slider(lookSec, { label: t('slider.look.linkCurve'), min: 0, max: 1, step: 0.05, defaultValue: d.look.linkCurve, get: () => s.look.linkCurve, set: (v) => (s.look.linkCurve = v), fmt: (v) => (v < 0.025 ? t('value.off') : v.toFixed(2)), onInput: () => this.tracked(cb.onLook) }),
			new Slider(lookSec, { label: t('slider.look.twinkle'), min: 0, max: 2, step: 0.1, defaultValue: d.look.twinkle, get: () => s.look.twinkle, set: (v) => (s.look.twinkle = v), fmt: (v) => (v < 0.05 ? t('value.off') : `${v.toFixed(1)}`), onInput: () => this.tracked(cb.onLook) }),
		);
		const sizeRow = lookSec.createDiv({ cls: 'galaxy-panel-row' });
		this.sizeByBtn = sizeRow.createEl('button', { text: this.sizeByLabel() });
		this.sizeByBtn.addEventListener('click', () => {
			const order: typeof s.look.sizeBy[] = ['degree', 'fileSize', 'uniform'];
			s.look.sizeBy = order[(order.indexOf(s.look.sizeBy) + 1) % order.length] ?? 'degree';
			this.sizeByBtn?.setText(this.sizeByLabel());
			this.tracked(cb.onSizeBy);
		});
		const themeSel = lookSec.createEl('select', { cls: 'gx-theme-select' });
		// The name was being carried by the first option, and the moment a theme is chosen that slot becomes the theme's name ——
		// from then on this select is a control with no name.  The label is written separately.
		themeSel.setAttribute('aria-label', t('look.theme.placeholder'));
		const customOpt = themeSel.createEl('option', { text: t('look.theme.placeholder'), value: '' });
		customOpt.disabled = true;
		for (const th of COLOR_THEMES) themeSel.createEl('option', { text: th.name, value: th.id });
		themeSel.value = COLOR_THEMES.some((th) => th.id === s.colorTheme) ? s.colorTheme : '';
		if (!themeSel.value) customOpt.selected = true;
		themeSel.addEventListener('change', () => {
			const th = COLOR_THEMES.find((x) => x.id === themeSel.value);
			if (th) {
				cb.onColorTheme(th);
				this.refreshMarkers();
			}
		});
		const colorRow = lookSec.createDiv({ cls: 'galaxy-panel-row' });
		colorRow.createEl('button', { text: t('look.import') }).addEventListener('click', () => {
			cb.onImportColors();
			customOpt.selected = true;
		});
		this.shuffleBtn = colorRow.createEl('button', { text: t('look.shuffle') });
		this.shuffleBtn.addEventListener('click', () => {
			cb.onShuffleColors();
			customOpt.selected = true;
		});
		this.syncShuffle();

		// The deep-space background (v0.4): a starfield toggle + three shape-layer sliders (0 = off), freely combined
		const spaceSec = section(SEC.space, 'panel.sec.space', 'space');
		const fmtOff = (v: number) => (v < 0.025 ? t('value.off') : v.toFixed(2));
		this.sliders.push(
			new Slider(spaceSec, { label: t('slider.space.nebula'), min: 0, max: 1, step: 0.05, defaultValue: d.space.nebula, get: () => s.space.nebula, set: (v) => (s.space.nebula = v), fmt: fmtOff, onInput: () => this.tracked(cb.onSpace) }),
			new Slider(spaceSec, { label: t('slider.space.fieldStars'), min: 0, max: 1, step: 0.05, defaultValue: d.space.fieldStars, get: () => s.space.fieldStars, set: (v) => (s.space.fieldStars = v), fmt: fmtOff, onInput: () => this.tracked(cb.onSpace) }),
			new Slider(spaceSec, { label: t('slider.space.clusterClouds'), min: 0, max: 1, step: 0.05, defaultValue: d.space.clusterClouds, get: () => s.space.clusterClouds, set: (v) => (s.space.clusterClouds = v), fmt: fmtOff, onInput: () => this.tracked(cb.onSpace) }),
		);
		const starRow = spaceSec.createDiv({ cls: 'galaxy-panel-row' });
		this.starfieldBtn = starRow.createEl('button', { text: this.starfieldLabel() });
		this.starfieldBtn.addEventListener('click', () => {
			s.showStarfield = !s.showStarfield;
			this.starfieldBtn?.setText(this.starfieldLabel());
			this.tracked(() => cb.onStarfield(s.showStarfield));
		});

		// Glow
		const bloomSec = section(SEC.bloom, 'panel.sec.bloom', 'bloom');
		this.sliders.push(
			new Slider(bloomSec, { label: t('slider.bloom.strength'), min: 0, max: 2.5, step: 0.05, defaultValue: d.bloom.strength, get: () => s.bloom.strength, set: (v) => (s.bloom.strength = v), onInput: () => this.tracked(cb.onBloom) }),
			new Slider(bloomSec, { label: t('slider.bloom.radius'), min: 0, max: 1.2, step: 0.05, defaultValue: d.bloom.radius, get: () => s.bloom.radius, set: (v) => (s.bloom.radius = v), onInput: () => this.tracked(cb.onBloom) }),
			new Slider(bloomSec, { label: t('slider.bloom.threshold'), min: 0, max: 1, step: 0.05, defaultValue: d.bloom.threshold, get: () => s.bloom.threshold, set: (v) => (s.bloom.threshold = v), onInput: () => this.tracked(cb.onBloom) }),
		);

		// Physics (after glow: lower frequency, more advanced)
		const phySec = section(SEC.physics, 'panel.sec.physics', 'physics');
		this.sliders.push(
			new Slider(phySec, { label: t('slider.phys.repel'), min: 20, max: 400, step: 5, defaultValue: d.physics.repel, get: () => s.physics.repel, set: (v) => (s.physics.repel = v), fmt: (v) => String(Math.round(v)), onInput: () => this.tracked(cb.onPhysics) }),
			new Slider(phySec, { label: t('slider.phys.linkDistance'), min: 20, max: 200, step: 5, defaultValue: d.physics.linkDistance, get: () => s.physics.linkDistance, set: (v) => (s.physics.linkDistance = v), fmt: (v) => String(Math.round(v)), onInput: () => this.tracked(cb.onPhysics) }),
			new Slider(phySec, { label: t('slider.phys.linkStrength'), min: 0.1, max: 2, step: 0.1, defaultValue: d.physics.linkStrength, get: () => s.physics.linkStrength, set: (v) => (s.physics.linkStrength = v), fmt: (v) => `${v.toFixed(1)}×`, onInput: () => this.tracked(cb.onPhysics) }),
			new Slider(phySec, { label: t('slider.phys.centerPull'), min: 0, max: 0.2, step: 0.005, defaultValue: d.physics.centerPull, get: () => s.physics.centerPull, set: (v) => (s.physics.centerPull = v), fmt: (v) => v.toFixed(3), onInput: () => this.tracked(cb.onPhysics) }),
			new Slider(phySec, { label: t('slider.phys.flatten'), min: 0, max: 0.8, step: 0.02, defaultValue: d.physics.flatten, get: () => s.physics.flatten, set: (v) => (s.physics.flatten = v), onInput: () => this.tracked(cb.onPhysics) }),
			new Slider(phySec, { label: t('slider.phys.coreGravity'), min: -0.1, max: 0.3, step: 0.005, defaultValue: d.physics.coreGravity, get: () => s.physics.coreGravity, set: (v) => (s.physics.coreGravity = v), fmt: (v) => v.toFixed(3), onInput: () => this.tracked(cb.onPhysics) }),
			new Slider(phySec, { label: t('slider.phys.spiral'), min: 0, max: 0.1, step: 0.005, defaultValue: d.physics.spiral, get: () => s.physics.spiral, set: (v) => (s.physics.spiral = v), fmt: (v) => v.toFixed(3), onInput: () => this.tracked(cb.onPhysics) }),
		);

		// —— the navigation and motion section (collapsible; expanded by default, remembering its open state) ——
		const navSec = body.createEl('details', { cls: 'gx-section gx-zone-section' });
		if (s.panelSections['nav'] !== false) navSec.setAttribute('open', '');
		const navSum = navSec.createEl('summary');
		navSum.createSpan({ cls: 'gx-caret', text: '▸' });
		navSum.createSpan({ text: t('zone.move') });
		navSec.addEventListener('toggle', () => cb.onSectionToggle('nav', navSec.open));
		const navBody = navSec.createDiv({ cls: 'gx-section-body' });
		// Automatic orbiting
		const ao = navBody.createDiv({ cls: 'gx-nav-block' });
		const aoh = ao.createDiv({ cls: 'gx-nav-head' });
		aoh.createSpan({ text: t('nav.autoOrbit') });
		this.cruiseBtn = aoh.createEl('button', { cls: 'gx-mini-toggle', text: s.cruise ? '●' : '○' });
		// The title span beside it had no relationship to the button at all, so the announced name was the single glyph '●'.
		// is-on is a state visible only to the eye, so it is exposed as aria-pressed as well.
		this.cruiseBtn.setAttribute('aria-label', t('nav.autoOrbit'));
		this.cruiseBtn.toggleClass('is-on', s.cruise);
		this.cruiseBtn.setAttribute('aria-pressed', String(s.cruise));
		this.cruiseBtn.addEventListener('click', () => {
			s.cruise = !s.cruise;
			this.cruiseBtn?.setText(s.cruise ? '●' : '○');
			this.cruiseBtn?.toggleClass('is-on', s.cruise);
			this.cruiseBtn?.setAttribute('aria-pressed', String(s.cruise));
			cb.onCruise(s.cruise);
		});
		ao.createDiv({ cls: 'gx-nav-sub', text: t('nav.autoOrbitSub') });
		this.sliders.push(new Slider(ao, { label: t('slider.cruise.speed'), min: 0.2, max: 3, step: 0.1, defaultValue: d.cruiseSpeed, get: () => s.cruiseSpeed, set: (v) => (s.cruiseSpeed = v), fmt: (v) => `${v.toFixed(1)}×`, onInput: cb.onCruiseSpeed }));

		// Wander: one-click ambient automatic touring (hidden on the mobile tier —— a flyby is heavy)
		if (!Platform.isMobile) {
			const tb = navBody.createDiv({ cls: 'gx-nav-block' });
			const tbh = tb.createDiv({ cls: 'gx-nav-head' });
			tbh.createSpan({ text: t('nav.wander') });
			this.tourPlayBtn = tbh.createEl('button', { cls: 'gx-play', text: `▶ ${t('tour.play')}` });
			this.tourPlayBtn.addEventListener('click', () => cb.onTourToggle());
			tb.createDiv({ cls: 'gx-nav-sub', text: t('nav.wanderSub') });
			this.sliders.push(new Slider(tb, { label: t('slider.tour.speed'), min: 0.2, max: 3, step: 0.1, defaultValue: d.tour.speed, get: () => s.tour.speed, set: (v) => (s.tour.speed = v), fmt: (v) => `${v.toFixed(1)}×`, onInput: cb.onTourSpeed }));
		}
		// Connect two notes: a pathfinding tool (pick a start and an end → fly the shortest link path)
		const ct = navBody.createDiv({ cls: 'gx-nav-block' });
		const cth = ct.createDiv({ cls: 'gx-nav-head' });
		cth.createSpan({ text: t('nav.connect') });
		cth.createEl('button', { cls: 'gx-play', text: t('nav.connectGo') }).addEventListener('click', () => cb.onConnectTwo());
		ct.createDiv({ cls: 'gx-nav-sub', text: t('nav.connectSub') });

		const replay = navBody.createEl('button', { cls: 'gx-textlink', text: t('nav.replay') });
		replay.addEventListener('click', cb.onReveal);

		// —— the bottom bar ——
		const footer = body.createDiv({ cls: 'gx-footer' });
		const fRow = footer.createDiv({ cls: 'galaxy-panel-row' });
		// It goes through a confirmation, so refreshAll cannot be called straight from here ——
		// redrawing the panel after the actual reset is onReset's job (resetLook).
		fRow.createEl('button', { cls: 'gx-textlike', text: t('adv.resetLook') }).addEventListener('click', () => cb.onReset());
		fRow.createEl('button', { text: t('preset.save') }).addEventListener('click', () => cb.onSavePreset());

		const advSec = footer.createEl('details', { cls: 'gx-section' });
		if (s.panelSections['advanced']) advSec.setAttribute('open', '');
		const advSum = advSec.createEl('summary');
		advSum.createSpan({ cls: 'gx-caret', text: '▸' });
		advSum.createSpan({ text: t('panel.sec.advanced') });
		advSec.addEventListener('toggle', () => cb.onSectionToggle('advanced', advSec.open));
		const advBody = advSec.createDiv({ cls: 'gx-section-body' });
		const qRow = advBody.createDiv({ cls: 'gx-seg' });
		// A mutually exclusive selection is **invisible to assistive technology through a CSS class
		// alone.**  Which one is on has to be machine-readable.
		qRow.setAttribute('role', 'radiogroup');
		qRow.setAttribute('aria-label', t('panel.sec.advanced'));
		const qOpts: { v: GalaxySettings['qualityOverride']; k: Parameters<typeof t>[0] }[] = [
			{ v: 'auto', k: 'q.auto' }, { v: 'high', k: 'q.high' }, { v: 'low', k: 'q.low' }, { v: 'mobile', k: 'q.mobile' },
		];
		for (const o of qOpts) {
			const b = qRow.createEl('button', { text: t(o.k) });
			b.setAttribute('role', 'radio');
			const mark = (on: boolean) => {
				b.toggleClass('is-on', on);
				b.setAttribute('aria-checked', String(on));
			};
			mark(s.qualityOverride === o.v);
			b.addEventListener('click', () => {
				s.qualityOverride = o.v;
				for (const c of Array.from(qRow.children)) {
					c.removeClass('is-on');
					c.setAttribute('aria-checked', 'false');
				}
				mark(true);
				cb.onQuality();
			});
		}
		advBody.createDiv({ cls: 'gx-sub', text: t('quality.autoSub') });
		// The unresolved/orphan toggles moved to the "filter" section; tag exploration is in its own Tags section.
		this.advStatsEl = advBody.createDiv({ cls: 'gx-adv-stats', text: '…' });
		if (__GALAXY_DEV__) {
			const devRow = advBody.createDiv({ cls: 'galaxy-panel-row' });
			for (const sc of ['S1', 'S2', 'S3'] as const) devRow.createEl('button', { text: sc }).addEventListener('click', () => cb.runScenario(sc));
		}

		this.refreshMarkers();
		this.fixButtonTypes();
	}

	/** A `<button>`'s default type is `submit`.  Of this panel's 32 buttons, only `MenuItem` gave a type.
	 *
	 *  It is harmless today —— there is no `<form>` anywhere in the repository (grep finds 0).  Which
	 *  is what makes it **latent**: rendered inside a form one day, any button submits it, and the
	 *  symptom presents as "the screen sometimes reloads", which is hard to trace.
	 *
	 *  Instead of fixing 32 sites individually, one place sweeps them.  A hand-maintained list
	 *  always misses one (twice in this session already: sync's directory list, verify's type vocabulary).
	 *
	 *  ⚠ This is **a one-shot snapshot**.  The old comment claimed "it covers buttons added later"
	 *    and that was false —— `buildPresets()` runs again after every save, move, delete and rename,
	 *    creating fresh buttons with no `type` (r4-ux round 5).  So it **has to be called wherever
	 *    the DOM is rebuilt.**  Today that is two places: the end of the constructor and the end of
	 *    `buildPresets()`.  Add a call anywhere new that rebuilds. */
	private fixButtonTypes(): void {
		this.root.querySelectorAll<HTMLButtonElement>('button:not([type])')
			.forEach((b) => { b.type = 'button'; });
	}

	// ---------- presets ----------

	private buildPresets(): void {
		const host = this.presetHost;
		if (!host) return;
		host.empty();
		const grid = host.createDiv({ cls: 'gx-cards' });
		for (const p of STYLE_PRESETS) this.makePresetCard(grid, p, false, 0);
		const custom = this.settings.customPresets;
		if (custom.length > 0) {
			host.createDiv({ cls: 'gx-preset-label', text: t('preset.mine') });
			const cg = host.createDiv({ cls: 'gx-cards' });
			custom.forEach((p, i) => this.makePresetCard(cg, p, true, i));
		}
		// The DOM was rebuilt, so sweep again (see fixButtonTypes's comment)
		this.fixButtonTypes();
	}

	private makePresetCard(grid: HTMLElement, p: StylePreset, isMine: boolean, idx: number): void {
		// A div (not a button): Obsidian's button defaults to inline-flex centring + a fixed height, which squashes or clips a card's multi-line content.
		const card = grid.createDiv({ cls: 'gx-pcard', attr: { role: 'button', tabindex: '0' } });
		card.toggleClass('is-active', p.id === this.settings.activePreset);
		const nameRow = card.createDiv({ cls: 'gx-pcard-n' });
		const icon = nameRow.createSpan({ cls: 'gx-pcard-icon' });
		try {
			drawPresetIcon(icon, isMine ? 'custom' : p.id, themeColor(p.theme));
		} catch {
			/* The icon is decorative; failing to draw it does not hurt the card */
		}
		const nameSpan = nameRow.createSpan({ text: presetName(p) });
		// A built-in preset's subtitle = a description of its character (information that distinguishes); a custom preset has none (so the same platitude is not printed on every card)
		if (!isMine) card.createDiv({ cls: 'gx-pcard-s', text: t(`preset.sub.${p.id}` as Parameters<typeof t>[0]) });
		if (isMine) {
			// The four management actions (rename/move up/move down/delete) go into one ⋯ menu: a single dot in the top right, neither crowding nor covering the name
			const ops = card.createDiv({ cls: 'gx-mine-ops' });
			const more = ops.createEl('button', { cls: 'gx-more', text: '⋯' });
			more.setAttribute('aria-label', t('mine.more'));
			more.setAttribute('aria-haspopup', 'menu');   // see the langBtn comment above
			more.setAttribute('aria-expanded', 'false');
			more.addEventListener('click', (e) => { e.stopPropagation(); this.openPresetMenu(e, idx, card, nameSpan, p); });
		}
		const commit = () => { this.cb.onStylePreset(p); this.refreshAll(); };
		card.addEventListener('mouseenter', () => { this.cb.onPresetHover(p); this.showPreviewMarkers(presetName(p)); });
		card.addEventListener('mouseleave', () => { this.cb.onPresetHoverEnd(); this.refreshMarkers(); });
		card.addEventListener('click', commit);
		card.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				commit();
			}
		});
	}

	/** A custom preset's ⋯ menu: rename / move up / move down / delete (management actions are infrequent, so a menu keeps buttons out of the card's top-right corner) */
	private openPresetMenu(e: MouseEvent, idx: number, card: HTMLElement, nameSpan: HTMLElement, p: StylePreset): void {
		const last = this.settings.customPresets.length - 1;
		const menu = new Menu();
		menu.addItem((i) => i.setTitle(t('mine.rename')).setIcon('pencil').onClick(() => this.beginRename(idx, card, nameSpan, p)));
		menu.addItem((i) => i.setTitle(t('mine.up')).setIcon('arrow-up').setDisabled(idx === 0).onClick(() => this.cb.onMovePreset(idx, -1)));
		menu.addItem((i) => i.setTitle(t('mine.down')).setIcon('arrow-down').setDisabled(idx === last).onClick(() => this.cb.onMovePreset(idx, 1)));
		menu.addSeparator();
		menu.addItem((i) => i.setTitle(t('mine.del')).setIcon('trash').onClick(() => this.cb.onDeletePreset(idx)));
		menu.showAtMouseEvent(e);
	}

	/** Inline rename: swap the name span in place for an input, committing on Enter/blur and cancelling on Esc; refreshPresets rebuilds the card on success */
	private beginRename(idx: number, card: HTMLElement, nameSpan: HTMLElement, p: StylePreset): void {
		const input = nameSpan.parentElement?.createEl('input', { cls: 'gx-pcard-rename', value: presetName(p) });
		if (!input) return;
		card.addClass('is-renaming'); // the editing state hides ✎↑↓×, and the input takes the whole row
		nameSpan.replaceWith(input);
		input.focus();
		input.select();
		let done = false;
		const finish = (save: boolean) => {
			if (done) return;
			done = true;
			const v = input.value.trim();
			if (save && v) this.cb.onRenamePreset(idx, v); // → refreshPresets rebuilds
			else this.buildPresets(); // cancelled or empty: rebuild, restoring the original name
		};
		// No keypress or click inside the input may bubble to the card (or it would apply the preset, or space would page)
		input.addEventListener('keydown', (e) => {
			e.stopPropagation();
			if (e.key === 'Enter') { e.preventDefault(); finish(true); }
			else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
		});
		input.addEventListener('click', (e) => e.stopPropagation());
		input.addEventListener('blur', () => finish(true));
	}

	/** Called by GraphController after a preset is saved, moved or deleted: rebuild the card area */
	refreshPresets(): void {
		this.buildPresets();
		this.refreshMarkers();
	}

	// ---------- section markers ----------

	/** A parameter change is wrapped through here: the callback + an immediate refresh of the section markers */
	private tracked(fn: () => void): void {
		fn();
		this.refreshMarkers();
	}

	private activePresetObj(): StylePreset | undefined {
		return [...STYLE_PRESETS, ...this.settings.customPresets].find((p) => p.id === this.settings.activePreset);
	}
	private near(a: number, b: number): boolean {
		return Math.abs(a - b) < 1e-4;
	}
	/**
	 * The "filter" section (#11): a clickable folder legend (primary) + a collapsed text query (the escape hatch) + the unresolved/orphan toggles.
	 *
	 * Why the legend is the main event: nodes have always been coloured by top-level folder, and the panel never exposed the legend ——
	 * the user saw clumps of colour, knowing neither what the colours meant nor how to do anything with them.  Making the legend clickable
	 * has one thing answer both "what does this colour mean" and "show me only this", with no syntax at all.  The text box is left for the
	 * cross-cutting patterns a legend cannot express.
	 *
	 * The copy is restrained: the section title already says what it is for, so the placeholder does not repeat "filter notes…" but gives a usable real query that teaches the syntax;
	 * the panel header already shows the note count, so there is no "showing N/M" —— only zero matches gets a message (an empty view looks like a crash).
	 */
	private buildFilterSection(body: HTMLElement, cb: ControlPanelCallbacks): void {
		const s = this.settings;
		const det = body.createEl('details', { cls: 'gx-section gx-zone-section' });
		if (s.panelSections['filter'] !== false) det.setAttribute('open', ''); // a new section is expanded by default (filtering is the main feature)
		const sum = det.createEl('summary');
		sum.createSpan({ cls: 'gx-caret', text: '▸' });
		sum.createSpan({ text: t('panel.sec.filter') });
		sum.createSpan({ cls: 'gx-sec-head-spacer' });
		det.addEventListener('toggle', () => cb.onSectionToggle('filter', det.open));
		const fBody = det.createDiv({ cls: 'gx-section-body' });
		// "Select all" appears only when some folders are switched off —— with nothing to restore it carries zero information.
		// It sits in the body rather than the summary, for the same reason as the section reset.
		this.filterAllBtn = fBody.createEl('button', { cls: 'gx-sec-restore', text: t('filter.all') });
		this.filterAllBtn.addEventListener('click', () => {
			s.hiddenFolders = [];
			cb.onHiddenFolders([]);
			this.refreshFolders();
		});

		// In entity mode there is a choice of what to group by.
		//   type       'BM25' is a concept, 'LanceDB' is a tool  — what it is
		//   community  both in the 'hybrid search' clump          — what it is used with
		// The measured modularity is 0.864, so the communities really are distinct (docs/PIPELINE.md).
		const grpRow = fBody.createDiv({ cls: 'gx-kdb-grp' });
		grpRow.setAttribute('role', 'radiogroup');
		grpRow.setAttribute('aria-label', t('kdb.groupBy'));
		this.grpRowEl = grpRow;
		if (!s.kdbMode) grpRow.addClass('is-hidden');
		const grpBtns: { el: HTMLElement; v: 'type' | 'community' }[] = [];
		const mkGrp = (label: string, v: 'type' | 'community') => {
			const b = grpRow.createEl('button', { text: label });
			b.setAttribute('role', 'radio');
			b.setAttribute('aria-checked', String(s.kdbGroupBy === v));
			grpBtns.push({ el: b, v });
			b.addEventListener('click', () => {
				if (s.kdbGroupBy === v) return;
				s.kdbGroupBy = v;
				this.repaintGrp?.();
				cb.onKdbGroupBy(v);
			});
			return b;
		};
		mkGrp('By type', 'type');
		mkGrp('By topic', 'community');
		// The button state is drawn from **the settings**, not from the click.  Refreshed only on
		// click, the highlight diverges from reality when the mode is changed programmatically.
		this.repaintGrp = () => {
			for (const g of grpBtns) {
				const on = s.kdbGroupBy === g.v;
				g.el.toggleClass('is-on', on);
				g.el.setAttribute('aria-checked', String(on));   // it has to move **with** the class
			}
			if (this.filterHintEl) {
				this.filterHintEl.setText(
					!s.kdbMode ? 'Toggle top-level folders on and off'
						: s.kdbGroupBy === 'community'
							? 'Toggle topics on and off (named by top 3 entities)'
							: 'Toggle entity types on and off',
				);
			}
		};
		this.repaintGrp();

		// The meaning of this list changes completely with the mode (folders ↔ entity types/communities).
		// The names alone do not distinguish them, so one line says which.
		this.filterHintEl = fBody.createDiv({ cls: 'gx-filter-hint' });
		this.filterHintEl.setText(
			this.settings.kdbMode ? 'Toggle entity types on and off' : 'Toggle top-level folders on and off',
		);

		// —— primary: the folder legend ——
		// The height is fixed.  Growing and shrinking with the content keeps pushing the list below
		// it, so sweeping the mouse makes the screen judder.  When empty it shows the overall summary.
		this.groupTipEl = fBody.createDiv({ cls: 'gx-gtip' });
		this.groupTipIdle = () => {
			const el = this.groupTipEl;
			if (!el) return;
			el.empty();
			el.addClass('is-idle');
			const list = this.cb.getFolders();
			const named = list.filter((f) => f.folder !== 'Other');
			const other = list.find((f) => f.folder === 'Other');
			// The same box serves folders, types and topics.  Pinning it to 'communities' would put
			// the false words "8 communities" on screen when viewing by type.
			const unit = !this.settings.kdbMode ? 'folder'
				: this.settings.kdbGroupBy === 'community' ? 'topic' : 'type';
			el.createDiv({ cls: 'gx-gtip-h', text: `${named.length} ${unit}s` });
			el.createDiv({
				cls: 'gx-gtip-s',
				text: `${named.reduce((a, f) => a + f.count, 0).toLocaleString()} entities`
					+ (other ? ` · ${other.count.toLocaleString()} more are 'other'` : ''),
			});
			el.createDiv({ cls: 'gx-gtip-m', text: `Hover a row to highlight only that ${unit}` });
		};
		this.folderHost = fBody.createDiv({ cls: 'gx-folders' });
		this.refreshFolders();

		this.filterNoneEl = fBody.createDiv({ cls: 'gx-filter-none' });
		this.filterNoneEl.toggleClass('is-hidden', true);

		// —— secondary: the text-query escape hatch (collapsed by default; for the cross-cutting patterns a legend cannot express) ——
		const esc = fBody.createDiv({ cls: 'gx-filter-esc' });
		const escToggle = esc.createDiv({ cls: 'gx-filter-esc-t' });
		escToggle.createSpan({ text: '＋' });
		escToggle.createSpan({ text: t('filter.byName') });
		const escBody = esc.createDiv({ cls: 'gx-filter-esc-b' });
		// A saved query expands it by default —— otherwise the user cannot see that an invisible condition is filtering them
		const startOpen = s.filterQuery.length > 0;
		escBody.toggleClass('is-open', startOpen);
		escToggle.toggleClass('is-open', startOpen);
		escToggle.addEventListener('click', () => {
			const open = !escBody.hasClass('is-open');
			escBody.toggleClass('is-open', open);
			escToggle.toggleClass('is-open', open);
			if (open) this.filterInput?.focus();
		});

		const wrap = escBody.createDiv({ cls: 'gx-filter' });
		const input = wrap.createEl('input', { cls: 'gx-filter-input', type: 'text' });
		input.placeholder = t('filter.placeholder');
		input.spellcheck = false;
		input.value = s.filterQuery;
		input.setAttr('title', t('filter.syntax'));
		this.filterInput = input;

		const clear = wrap.createEl('button', { cls: 'gx-filter-clear', text: '✕' });
		clear.setAttr('aria-label', t('filter.clear'));
		clear.setAttr('title', t('filter.clear'));
		clear.toggleClass('is-hidden', s.filterQuery.length === 0);

		const push = (v: string) => {
			s.filterQuery = v;
			clear.toggleClass('is-hidden', v.length === 0);
			cb.onFilter(v);
		};
		input.addEventListener('input', () => push(input.value));
		// Esc clears (an Esc inside the input must not bubble up and cancel the node selection)
		input.addEventListener('keydown', (e) => {
			if (e.key !== 'Escape') return;
			e.stopPropagation();
			if (input.value.length === 0) return;
			input.value = '';
			push('');
		});
		clear.addEventListener('click', () => {
			input.value = '';
			push('');
			input.focus();
		});

		const row = fBody.createDiv({ cls: 'galaxy-panel-row' });
		this.unresolvedBtn = row.createEl('button', { text: this.unresolvedLabel() });
		this.unresolvedBtn.addEventListener('click', () => {
			s.showUnresolved = !s.showUnresolved;
			this.unresolvedBtn?.setText(this.unresolvedLabel());
			cb.onShowUnresolved(s.showUnresolved);
		});
		this.orphanBtn = row.createEl('button', { text: this.orphanLabel() });
		this.orphanBtn.addEventListener('click', () => {
			s.showOrphans = !s.showOrphans;
			this.orphanBtn?.setText(this.orphanLabel());
			cb.onShowOrphans(s.showOrphans);
		});
	}

	/** The master switch for tag data + Lens/colour/bounded hubs; all three visual toggles leave the graph unchanged by default. */
	private buildTagsSection(body: HTMLElement, cb: ControlPanelCallbacks): void {
		const s = this.settings;
		const det = body.createEl('details', { cls: 'gx-section gx-zone-section' });
		if (s.panelSections['tags'] === true || (s.panelSections['tags'] === undefined && s.showTags)) det.setAttribute('open', '');
		const sum = det.createEl('summary');
		sum.createSpan({ cls: 'gx-caret', text: '▸' });
		sum.createSpan({ text: t('panel.sec.tags') });
		det.addEventListener('toggle', () => cb.onSectionToggle('tags', det.open));
		const tagBody = det.createDiv({ cls: 'gx-section-body' });
		const row = tagBody.createDiv({ cls: 'galaxy-panel-row' });
		this.tagBtn = row.createEl('button', { text: this.tagLabel() });
		this.tagBtn.addEventListener('click', () => {
			s.showTags = !s.showTags;
			if (!s.showTags) s.tagLens = null;
			cb.onShowTags(s.showTags);
			this.refreshTags();
		});
		this.tagOptionsHost = tagBody.createDiv({ cls: 'gx-tag-options' });
		const optionsRow = this.tagOptionsHost.createDiv({ cls: 'galaxy-panel-row' });
		this.tagColorBtn = optionsRow.createEl('button', { text: this.tagColorLabel() });
		this.tagColorBtn.addEventListener('click', () => {
			s.colorByTag = !s.colorByTag;
			cb.onTagColorMode();
			this.refreshTags();
		});
		this.tagHubsBtn = optionsRow.createEl('button', { text: this.tagHubsLabel() });
		this.tagHubsBtn.addEventListener('click', () => {
			s.showTagHubs = !s.showTagHubs;
			cb.onTagHubs();
			this.refreshTags();
		});
		this.tagHubSliderHost = this.tagOptionsHost.createDiv({ cls: 'gx-tag-hub-slider' });
		this.sliders.push(new Slider(this.tagHubSliderHost, {
			label: t('tag.hubs.limit'),
			min: 5,
			max: 50,
			step: 1,
			defaultValue: DEFAULT_SETTINGS.tagHubLimit,
			get: () => s.tagHubLimit,
			set: (v) => (s.tagHubLimit = v),
			fmt: (v) => String(Math.round(v)),
			onInput: cb.onTagHubLimit,
		}));
		this.tagOptionsHost.createEl('button', { cls: 'gx-textlink', text: t('tag.reset') }).addEventListener('click', () => {
			cb.onResetTags();
			this.refreshTags();
		});
		this.tagHost = tagBody.createDiv({ cls: 'gx-tags' });
		this.refreshTags();
	}

	/** Redraw the tag chips after a data rebuild, a Lens switch or a settings sync. */
	refreshTags(): void {
		this.tagBtn?.setText(this.tagLabel());
		this.tagColorBtn?.setText(this.tagColorLabel());
		this.tagHubsBtn?.setText(this.tagHubsLabel());
		this.tagOptionsHost?.toggleClass('is-hidden', !this.settings.showTags);
		this.tagHubSliderHost?.toggleClass('is-hidden', !this.settings.showTags || !this.settings.showTagHubs);
		const host = this.tagHost;
		if (!host) return;
		host.empty();
		host.toggleClass('is-hidden', !this.settings.showTags);
		if (!this.settings.showTags) return;
		for (const tag of this.cb.getTopTags()) {
			const chip = host.createEl('button', { cls: 'gx-tag-chip' });
			const active = this.settings.tagLens === tag.id;
			chip.toggleClass('is-active', active);
			chip.setAttribute('aria-pressed', String(active));
			chip.createSpan({ cls: 'gx-tag-name', text: tag.name });
			chip.createSpan({ cls: 'gx-tag-count', text: String(tag.count) });
			chip.addEventListener('click', () => {
				this.cb.onTagLens(tag.id);
			});
		}
	}

	/** Called by GraphController after a rebuild: a message only on zero matches, blank otherwise (the header's note count is already moving) */
	setFilterEmpty(empty: boolean): void {
		if (!this.filterNoneEl) return;
		this.filterNoneEl.setText(empty ? t('filter.none') : '');
		this.filterNoneEl.toggleClass('is-hidden', !empty);
	}

	/**
	 * With fewer than 2 colour groups there is nothing to shuffle (it has to match the floor in GraphController.shuffleColors).
	 * A button that pretends to be alive and refuses every time reads as broken —— if it cannot be used, it says so first.
	 * Every path that changes colorGroups goes through applyColorFn, so the refresh is received there.
	 */
	syncShuffle(): void {
		const btn = this.shuffleBtn;
		if (!btn) return;
		const ready = this.settings.colorGroups.length >= 2;
		btn.disabled = !ready;
		btn.title = ready ? '' : t('notice.needImport');
	}

	/** Redraw the folder legend.  Both the data and the colours come live through callbacks —— the panel builds in the constructor, and field injection arrives a step later */
	refreshFolders(): void {
		const host = this.folderHost;
		if (!host) return;
		host.empty();
		const hidden = new Set(this.settings.hiddenFolders);
		const list = this.cb.getFolders();
		for (const { folder, count } of list) {
			const label = folder === '' ? t('filter.rootFolder') : folder;
			// The row itself is no longer pressable.  On/off and "only" each need to be a <button>,
			// and a button inside a button is invalid, so the row stays a shell holding the hover background and the alignment.
			const row = host.createDiv({ cls: 'gx-folder-row' });
			row.toggleClass('is-off', hidden.has(folder));
			const chip = row.createEl('button', { cls: 'gx-folder' });
			// Pressed = on (visible on the graph).  The inverse of is-off —— a dimmed chip read as
			// pressed would make the screen and the announcement say opposite things.
			chip.setAttribute('aria-pressed', String(!hidden.has(folder)));
			const dot = chip.createSpan({ cls: 'gx-folder-dot' });
			dot.style.setProperty('--gx-folder-color', this.cb.folderColorHex(folder));
			const nameWrap = chip.createDiv({ cls: 'gx-folder-nm' });
			nameWrap.createSpan({ cls: 'gx-folder-name', text: label });
			// For a community, the representative entities as a subtitle.  The LLM's name alone hides 'what is actually in it'
			const info = this.cb.groupInfo(folder);
			if (info?.members) nameWrap.createSpan({ cls: 'gx-folder-sub', text: info.members });
			chip.createSpan({ cls: 'gx-folder-count', text: String(count) });

			// hover → leave only that community on the graph and settle the rest + the summary at the top
			row.addEventListener('mouseenter', () => {
				this.cb.onGroupHover(folder);
				this.showGroupTip(folder);
			});
			row.addEventListener('mouseleave', () => {
				this.cb.onGroupHover(null);
				this.groupTipIdle?.();
			});

			// "Only" = switch every other one off.  It appears on hover because it is an accelerator, not the main action
			// (arriving by keyboard, :focus-within shows it instead —— the same way as .gx-mine-ops)
			const solo = row.createEl('button', { cls: 'gx-folder-solo', text: t('filter.solo') });
			solo.setAttr('title', t('filter.soloTip'));
			// Eight rows all reading the single word "only" cannot be told apart by announcement alone.
			// The visible text is kept in front so voice control keeps working too.
			solo.setAttr('aria-label', `${t('filter.solo')} · ${label}`);
			solo.addEventListener('click', () => {
				const others = list.map((f) => f.folder).filter((f) => f !== folder);
				// Already "only this" → pressing again restores, so it is not a dead end
				const isSolo = others.every((f) => hidden.has(f)) && !hidden.has(folder);
				this.applyHidden(isSolo ? [] : others);
			});

			chip.addEventListener('click', () => {
				const next = new Set(hidden);
				next.has(folder) ? next.delete(folder) : next.add(folder);
				this.applyHidden([...next]);
			});
		}
		this.groupTipIdle?.();
		// gx-hide rather than is-hidden: .gx-sec-restore's show/hide rule is gx-hide (an existing convention, see styles.css)
		this.filterAllBtn?.toggleClass('gx-hide', hidden.size === 0);
	}

	/** Refresh the community summary box.  null returns it to the default (the overall summary).  A graph hover calls this too. */
	showGroupTip(folder: string | null): void {
		const el = this.groupTipEl;
		if (!el) return;
		if (folder === null) { this.groupTipIdle?.(); return; }
		const info = this.cb.groupInfo(folder);
		el.empty();
		el.removeClass('is-idle');
		el.createDiv({ cls: 'gx-gtip-h', text: folder });
		if (info?.summary) el.createDiv({ cls: 'gx-gtip-s', text: info.summary });
		if (info?.members) el.createDiv({ cls: 'gx-gtip-m', text: info.members });
	}

	private applyHidden(next: string[]): void {
		this.settings.hiddenFolders = next;
		this.cb.onFilterSaved?.();     // store it into the active namespace
		this.cb.onHiddenFolders(next);
		this.refreshFolders();
	}

	private sectionClean(group: 'look' | 'physics' | 'bloom' | 'space'): boolean {
		const p = this.activePresetObj();
		if (!p) return false;
		const s = this.settings;
		if (group === 'bloom') return this.near(s.bloom.strength, p.bloom.strength) && this.near(s.bloom.radius, p.bloom.radius) && this.near(s.bloom.threshold, p.bloom.threshold);
		if (group === 'physics') return (['repel', 'linkDistance', 'linkStrength', 'centerPull', 'flatten', 'coreGravity', 'spiral'] as const).every((k) => this.near(s.physics[k], p.physics[k]));
		if (group === 'space') return (['nebula', 'fieldStars', 'clusterClouds'] as const).every((k) => this.near(s.space[k], p.space[k])) && s.showStarfield === p.starfield;
		return this.near(s.look.nodeSize, p.look.nodeSize) && this.near(s.look.linkOpacity, p.look.linkOpacity) && this.near(s.look.linkCurve, p.look.linkCurve) && this.near(s.look.twinkle, p.look.twinkle) && s.look.sizeBy === p.look.sizeBy && s.colorTheme === p.theme;
	}

	private refreshMarkers(): void {
		const p = this.activePresetObj();
		for (const def of SECTION_DEFS) {
			const reg = this.secBadges[def.id];
			if (!reg) continue;
			if (!p) {
				reg.badge.setText('');
				reg.badge.className = 'gx-sec-badge';
				reg.restore.toggleClass('gx-hide', true);
				continue;
			}
			const clean = this.sectionClean(def.group);
			reg.badge.setText(clean ? t('sec.setBy', { n: presetName(p) }) : t('sec.customized'));
			reg.badge.className = 'gx-sec-badge' + (clean ? '' : ' is-dirty');
			reg.restore.toggleClass('gx-hide', clean);
		}
	}

	private showPreviewMarkers(name: string): void {
		for (const def of SECTION_DEFS) {
			const reg = this.secBadges[def.id];
			if (!reg) continue;
			reg.badge.setText(t('sec.preview', { n: name }));
			reg.badge.className = 'gx-sec-badge is-preview';
			reg.restore.toggleClass('gx-hide', true);
		}
	}

	// ---------- the help overlay ----------

	private toggleHelp(anchor: HTMLElement): void {
		if (this.helpEl) {
			this.helpEl.remove();
			this.helpEl = null;
			return;
		}
		const pop = anchor.createDiv({ cls: 'gx-help-pop' });
		pop.createEl('h4', { text: t('help.title') });
		const lines = pop.createDiv({ cls: 'gx-help-lines' });
		for (const key of ['help.orbit', 'help.pan', 'help.ctrlClick', 'help.wasd', 'help.select', 'help.keys', 'help.dblclick'] as const) {
			lines.createDiv({ text: t(key) });
		}
		this.helpEl = pop;
	}

	// ---------- tags ----------

	private sizeByLabel(): string {
		const m = this.settings.look.sizeBy;
		return m === 'degree' ? t('sizeBy.degree') : m === 'fileSize' ? t('sizeBy.fileSize') : t('sizeBy.uniform');
	}
	private starfieldLabel(): string {
		return this.settings.showStarfield ? t('look.starfield.on') : t('look.starfield.off');
	}
	private unresolvedLabel(): string {
		return this.settings.showUnresolved ? t('adv.unresolved.on') : t('adv.unresolved.off');
	}
	private orphanLabel(): string {
		return this.settings.showOrphans ? t('adv.orphan.on') : t('adv.orphan.off');
	}
	private tagLabel(): string {
		return this.settings.showTags ? t('adv.tag.on') : t('adv.tag.off');
	}
	private tagColorLabel(): string {
		return this.settings.colorByTag ? t('tag.color.on') : t('tag.color.off');
	}
	private tagHubsLabel(): string {
		return this.settings.showTagHubs ? t('tag.hubs.on') : t('tag.hubs.off');
	}

	refreshAll(): void {
		for (const sl of this.sliders) sl.refresh();
		this.cruiseBtn?.setText(this.settings.cruise ? '●' : '○');
		this.cruiseBtn?.toggleClass('is-on', this.settings.cruise);
		this.unresolvedBtn?.setText(this.unresolvedLabel());
		this.orphanBtn?.setText(this.orphanLabel());
		this.tagBtn?.setText(this.tagLabel());
		this.refreshTags();
		this.sizeByBtn?.setText(this.sizeByLabel());
		this.starfieldBtn?.setText(this.starfieldLabel());
		this.buildPresets();
		this.refreshMarkers();
	}

	setTourRunning(on: boolean): void {
		this.tourPlayBtn?.setText(on ? `■ ${t('tour.stop')}` : `▶ ${t('tour.play')}`);
	}

	/** Drag the right edge to resize (240–480px), persisted on release */
	private buildResizer(cb: ControlPanelCallbacks): void {
		const grip = this.root.createDiv({ cls: 'gx-resizer' });
		grip.addEventListener('pointerdown', (e) => {
			e.preventDefault();
			grip.setPointerCapture(e.pointerId);
			const startX = e.clientX;
			const startW = this.root.getBoundingClientRect().width;
			const move = (ev: PointerEvent) => {
				const w = Math.min(Math.max(startW + (ev.clientX - startX), 240), 480);
				this.root.style.width = `${w}px`;
			};
			const up = (ev: PointerEvent) => {
				grip.releasePointerCapture(ev.pointerId);
				grip.removeEventListener('pointermove', move);
				grip.removeEventListener('pointerup', up);
				cb.onPanelWidth(this.root.getBoundingClientRect().width);
			};
			grip.addEventListener('pointermove', move);
			grip.addEventListener('pointerup', up);
		});
	}

	/** The header language button's short label: zh uses the Han glyph, the rest an uppercase code (EN/DE/IT/ES/PT) */
	private langLabel(l: Lang): string {
		return l === 'zh' ? '中' : l.toUpperCase();
	}

	/** The language menu: auto + six languages, with the current preference ticked */
	private openLangMenu(e: MouseEvent, cb: ControlPanelCallbacks): void {
		const menu = new Menu();
		menu.addItem((i) => i.setTitle(t('set.language.auto')).setChecked(this.settings.language === 'auto').onClick(() => cb.onLanguage('auto')));
		for (const l of LANGS) {
			menu.addItem((i) => i.setTitle(l.name).setChecked(this.settings.language === l.code).onClick(() => cb.onLanguage(l.code)));
		}
		menu.showAtMouseEvent(e);
	}

	setPanelTheme(cls: 'gx-theme-dark' | 'gx-theme-light'): void {
		this.root.removeClass('gx-theme-dark');
		this.root.removeClass('gx-theme-light');
		this.root.addClass(cls);
	}

	dispose(): void {
		this.root.remove();
		this.sliders = [];
		this.secBadges = {};
	}
}
