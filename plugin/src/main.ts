import { Notice, Plugin } from 'obsidian';
import { VIEW_TYPE_GALAXY } from './constants';
import type { GalaxySettings } from './settings';
import { DEFAULT_SETTINGS, mergeSettings } from './settings';
import { resolveLang, setLang, t } from './i18n';
import { GalaxySettingTab } from './settings/SettingsTab';
import { GalaxyView } from './view/GalaxyView';
import { heapUsed, sleep, writeBenchResult } from './bench/bench';

export default class GalaxyViewPlugin extends Plugin {
	settings: GalaxySettings = DEFAULT_SETTINGS;

	async onload(): Promise<void> {
		this.settings = mergeSettings(await this.loadData());
		setLang(resolveLang(this.settings.language)); // the command names and the ribbon need the language decided before registration
		this.registerView(VIEW_TYPE_GALAXY, (leaf) => new GalaxyView(leaf, this));
		this.addSettingTab(new GalaxySettingTab(this.app, this));

		this.addRibbonIcon('orbit', t('cmd.open'), () => {
			void this.activateView();
		});

		this.addCommand({
			id: 'open',
			name: t('cmd.open'),
			callback: () => void this.activateView(),
		});

		this.addCommand({
			id: 'search',
			name: t('cmd.search'),
			callback: () => {
				void this.activateView().then((view) => view?.controller?.openSearch());
			},
		});

		this.addCommand({
			id: 'tour',
			name: t('cmd.tour'),
			callback: () => {
				void this.activateView().then((view) => view?.controller?.toggleTour());
			},
		});

		if (!__GALAXY_DEV__) return; // the benchmark commands below are development-time only, stripped from a store build

		this.addCommand({
			id: 'bench-suite',
			name: 'Benchmark: orbit, cold layout, unresolved included',
			callback: () => void this.runBenchSuite(),
		});

		this.addCommand({
			id: 'bench-leak',
			name: 'Benchmark: leak detection (open and close the view repeatedly)',
			callback: () => void this.runLeakCanary(),
		});
	}

	async saveSettings(): Promise<void> {
		await this.saveData(this.settings);
	}

	async activateView(): Promise<GalaxyView | null> {
		const { workspace } = this.app;
		let leaf = workspace.getLeavesOfType(VIEW_TYPE_GALAXY)[0] ?? null;
		if (!leaf) {
			leaf = workspace.getLeaf(true);
			await leaf.setViewState({ type: VIEW_TYPE_GALAXY, active: true });
		}
		if (leaf.isDeferred) await leaf.loadIfDeferred();
		await workspace.revealLeaf(leaf);
		return leaf.view instanceof GalaxyView ? leaf.view : null;
	}

	private async runBenchSuite(): Promise<void> {
		const view = await this.activateView();
		if (!view) {
			new Notice(t('notice.openFail'));
			return;
		}
		// Wait for the controller to finish starting asynchronously
		for (let i = 0; i < 100 && !view.controller; i++) await sleep(100);
		const c = view.controller;
		if (!c) {
			new Notice(t('notice.initTimeout'));
			return;
		}
		await c.runScenario('S1');
		await c.runScenario('S2');
		await c.runScenario('S3');
		new Notice('Benchmark done — results in _galaxy_bench/');
	}

	/** S4: open and close the view ×10 and watch the heap growth and the WebGL context warnings (the latter in the console) */
	private async runLeakCanary(): Promise<void> {
		this.app.workspace.detachLeavesOfType(VIEW_TYPE_GALAXY);
		await sleep(500);
		const before = heapUsed();
		let counts = { nodes: 0, links: 0 };
		const cycles = 10;
		for (let i = 0; i < cycles; i++) {
			const view = await this.activateView();
			await sleep(2000);
			if (view) counts = view.counts;
			this.app.workspace.detachLeavesOfType(VIEW_TYPE_GALAXY);
			await sleep(400);
			new Notice(`S4：${i + 1}/${cycles}`);
		}
		// A layout tick produces a lot of short-lived garbage (d3 rebuilds the octree every tick), and a full heap collection
		// does not necessarily run during a busy loop —— read the figure after the idle collection has finished, or collection lag is mistaken for a leak (measured 2026-06-12)
		new Notice('Benchmark: waiting for GC…');
		await sleep(20_000);
		const after = heapUsed();
		const result = {
			scenario: 'S4',
			timestamp: new Date().toISOString(),
			nodes: counts.nodes,
			links: counts.links,
			bloom: true,
			renderer: 'aggregate',
			cycles,
			heapBeforeMB: before / 1048576,
			heapAfterMB: after / 1048576,
			heapDeltaMB: (after - before) / 1048576,
			note: 'The WebGL context warnings need the developer console; a real leak means "before" rising across two consecutive rounds',
		};
		await writeBenchResult(this.app, result);
		new Notice(`S4 done: heap delta ${result.heapDeltaMB.toFixed(1)} MB (pass < 20MB)`);
	}
}
