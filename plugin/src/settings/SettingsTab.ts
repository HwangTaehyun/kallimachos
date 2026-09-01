// The classic PluginSettingTab (overriding display): the declarative getSettingDefinitions needs 1.13.0 and our floor is 1.8.7, so the classic API it is.
// To touch no "deprecated call" at all: internal redraws go through the private render() (rather than calling display() again), and the destroy button uses the mod-warning class (rather than setWarning()).
import { Notice, PluginSettingTab, Setting } from 'obsidian';
import type { App } from 'obsidian';
import type GalaxyViewPlugin from '../main';
import { VIEW_TYPE_GALAXY } from '../constants';
import { GalaxyView } from '../view/GalaxyView';
import { mergeSettings } from '../settings';
import { resolveLang, setLang, t, LANGS } from '../i18n';
import type { LangPref } from '../i18n';

/**
 * The settings page carries the "durable preferences": language, quality, visual mode, showing orphans and unresolved, and reset everything.
 * Live adjustments (sliders, style, palette, cruise) stay in the floating panel on the canvas.  Both read and write the same settings.
 */
export class GalaxySettingTab extends PluginSettingTab {
	constructor(
		app: App,
		private plugin: GalaxyViewPlugin,
	) {
		super(app, plugin);
	}

	/** Push a change out to every open galaxy view */
	private eachView(fn: (v: GalaxyView) => void): void {
		for (const leaf of this.app.workspace.getLeavesOfType(VIEW_TYPE_GALAXY)) {
			if (leaf.view instanceof GalaxyView) fn(leaf.view);
		}
	}

	display(): void {
		this.render();
	}

	private render(): void {
		const { containerEl } = this;
		containerEl.empty();
		const s = this.plugin.settings;

		// ── Knowledge DB ──
		// Nodes as entities rather than pages.  The data read is the
		// .obsidian/plugins/kal-galaxy/kal-graph.json that export_kal_graph.py produced.
		new Setting(containerEl).setName('Knowledge DB').setHeading();
		new Setting(containerEl)
			.setName('Entity graph mode')
			.setDesc('Draws LanceDB entities and relations instead of note links. Requires kal-graph.json.')
			.addToggle((tg) =>
				tg.setValue(s.kdbMode).onChange((v) => {
					s.kdbMode = v;
					void this.plugin.saveSettings();
					this.eachView((view) => void view.setKdbMode(v));
				}),
			);
		new Setting(containerEl)
			.setName('Min degree')
			.setDesc(
				'Hides entities with fewer links than this. 1 = everything. ' +
					'Measured distribution: median 2 · p90 6 · max 117 — most entities appear once or twice, ' +
					'so 2–5 makes the structure readable, and 20+ leaves only hubs.',
			)
			.addSlider((sl) =>
				sl
					.setLimits(1, 40, 1)
					.setValue(s.kdbMinDegree)
					.setDynamicTooltip()
					.onChange((v) => {
						s.kdbMinDegree = v;
						void this.plugin.saveSettings();
						this.eachView((view) => void view.setKdbMinDegree(v));
					}),
			);

		new Setting(containerEl)
			.setName('Reload graph data')
			.setDesc('Click after exporting a fresh kal-graph.json.')
			.addButton((b) =>
				b.setButtonText('Reload').onClick(() => {
					this.eachView((view) => void view.reloadKdb());
					new Notice('kal-graph.json reloaded');
				}),
			);

		new Setting(containerEl)
			.setName(t('set.language'))
			.setDesc(t('set.language.desc'))
			.addDropdown((d) => {
				d.addOption('auto', t('set.language.auto'));
				for (const l of LANGS) d.addOption(l.code, l.name); // en/zh/de/it/es/pt（endonym）
				d.setValue(s.language).onChange(async (v) => {
					s.language = (['auto', 'en', 'zh', 'de', 'it', 'es', 'pt'] as const).includes(v as 'auto') ? (v as LangPref) : 'auto';
					setLang(resolveLang(s.language));
					await this.plugin.saveSettings();
					this.eachView((view) => view.controller?.rebuildPanel());
					new Notice(t('set.langChanged'));
					this.render(); // redraw this page in the new language
				});
			});

		new Setting(containerEl)
			.setName(t('set.quality'))
			.setDesc(t('set.quality.desc'))
			.addDropdown((d) =>
				d
					.addOption('auto', t('q.auto'))
					.addOption('high', t('q.high'))
					.addOption('low', t('q.low'))
					.addOption('mobile', t('q.mobile'))
					.setValue(s.qualityOverride)
					.onChange(async (v) => {
						s.qualityOverride = v === 'high' || v === 'low' || v === 'mobile' ? v : 'auto';
						await this.plugin.saveSettings();
						this.eachView((view) => view.controller?.syncFromSettings());
					}),
			);

		new Setting(containerEl)
			.setName(t('set.preset'))
			.setDesc(t('set.preset.desc'))
			.addDropdown((d) =>
				d
					.addOption('deep-space', t('set.preset.deepSpace'))
					.addOption('adaptive', t('set.preset.adaptive'))
					.setValue(s.preset)
					.onChange(async (v) => {
						s.preset = v === 'adaptive' ? 'adaptive' : 'deep-space';
						await this.plugin.saveSettings();
						this.eachView((view) => view.controller?.syncFromSettings());
					}),
			);

		new Setting(containerEl)
			.setName(t('set.orphans'))
			.setDesc(t('set.orphans.desc'))
			.addToggle((tg) =>
				tg.setValue(s.showOrphans).onChange(async (v) => {
					s.showOrphans = v;
					await this.plugin.saveSettings();
					this.eachView((view) => view.controller?.syncFromSettings());
				}),
			);

		new Setting(containerEl)
			.setName(t('set.unresolved'))
			.setDesc(t('set.unresolved.desc'))
			.addToggle((tg) =>
				tg.setValue(s.showUnresolved).onChange(async (v) => {
					s.showUnresolved = v;
					await this.plugin.saveSettings();
					this.eachView((view) => view.controller?.syncFromSettings());
				}),
			);

		new Setting(containerEl)
			.setName(t('set.ghostEdges'))
			.setDesc(t('set.ghostEdges.desc'))
			.addToggle((tg) =>
				tg.setValue(s.showGhostEdges).onChange(async (v) => {
					s.showGhostEdges = v;
					await this.plugin.saveSettings();
					this.eachView((view) => view.controller?.syncFromSettings());
				}),
			);

		new Setting(containerEl)
			.setName(t('set.starfield'))
			.setDesc(t('set.starfield.desc'))
			.addToggle((tg) =>
				tg.setValue(s.showStarfield).onChange(async (v) => {
					s.showStarfield = v;
					await this.plugin.saveSettings();
					this.eachView((view) => view.controller?.syncFromSettings());
				}),
			);

		new Setting(containerEl)
			.setName(t('set.reset'))
			.setDesc(t('set.reset.desc'))
			.addButton((b) => {
				b.setButtonText(t('set.reset.cta'));
				b.buttonEl.addClass('mod-warning'); // the non-deprecated equivalent of setWarning(): add the destructive colour class directly
				b.onClick(async () => {
					// mergeSettings reconstructs every nested object (avoiding a shared reference with DEFAULT_SETTINGS);
					// the language and the warm-start coordinate cache are kept and the rest returns to the defaults.  An in-place Object.assign lets an already-open view see the update.
					const fresh = mergeSettings({ language: s.language, positionCache: s.positionCache });
					Object.assign(this.plugin.settings, fresh);
					await this.plugin.saveSettings();
					this.eachView((view) => view.controller?.syncFromSettings());
					this.render();
				});
			});
	}
}
