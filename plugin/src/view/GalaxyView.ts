import { ItemView, WorkspaceLeaf } from 'obsidian';
import { VIEW_TYPE_GALAXY } from '../constants';
import { t } from '../i18n';
import type { SettingsHost } from '../settings';
import { GraphController } from './GraphController';

export class GalaxyView extends ItemView {
	/** Called from the settings page — switch between the entity graph and the note graph */
	async setKdbMode(on: boolean): Promise<void> {
		await this.controller?.setKdbMode(on);
	}

	async reloadKdb(): Promise<void> {
		await this.controller?.reloadKdb();
	}

	async setKdbMinDegree(v: number): Promise<void> {
		await this.controller?.setKdbMinDegree(v);
	}

	navigation = true;
	controller: GraphController | null = null;

	constructor(
		leaf: WorkspaceLeaf,
		private host: SettingsHost,
	) {
		super(leaf);
	}

	getViewType(): string {
		return VIEW_TYPE_GALAXY;
	}

	getDisplayText(): string {
		// It has to read the same as manifest.json's name —— when the tab title and the plugin list
		// differ there is no telling they are the same thing.  The language has to follow too.
		return t('view.title');
	}

	getIcon(): string {
		return 'orbit';
	}

	get counts(): { nodes: number; links: number } {
		return this.controller?.counts ?? { nodes: 0, links: 0 };
	}

	async onOpen(): Promise<void> {
		this.contentEl.empty();
		this.contentEl.addClass('galaxy-view-content');
		this.registerEvent(this.app.workspace.on('css-change', () => this.controller?.onCssChange()));
		// WebGL initialisation is deferred to the first non-zero size (a deferred or restored layout can be 0×0 at onOpen)
		this.tryInit();
	}

	onResize(): void {
		if (!this.controller) {
			this.tryInit();
			return;
		}
		this.controller.resize();
	}

	private tryInit(): void {
		if (this.controller) return;
		const { clientWidth: w, clientHeight: h } = this.contentEl;
		if (w < 10 || h < 10) return;
		const controller = new GraphController(this.app, this.contentEl, this.host.settings, () => void this.host.saveSettings());
		controller.onContextLost = () => this.rebuild();
		this.controller = controller;
		this.addChild(controller.store); // the Component lifecycle: registerEvent cleans up automatically
		void controller.start();
	}

	/** A full rebuild after the WebGL context is lost */
	private rebuild(): void {
		this.controller?.dispose();
		this.controller = null;
		this.contentEl.empty();
		this.tryInit();
	}

	async onClose(): Promise<void> {
		this.controller?.dispose();
		this.controller = null;
		this.contentEl.empty();
	}
}
