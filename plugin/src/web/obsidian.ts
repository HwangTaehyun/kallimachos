/**
 * The browser stand-in (shim) for the 'obsidian' module.
 *
 * Why this exists
 *   To bring galaxy-view up **outside** Obsidian, unchanged.  Using the fork's renderer, layout
 *   and UI without editing a line means swapping the 'obsidian' symbols that code imports for
 *   ones that work in a browser.
 *   An esbuild alias points `obsidian` at this file (esbuild.web.mjs).
 *
 * What is filled in — only what is actually used
 *
 *   The rest (Plugin/ItemView/PluginSettingTab/Setting) is unused by the web entry point but
 *   kept in minimal form so type resolution does not break.
 *
 * Limits
 *   Vault events never fire.  A browser has no file watching, and the graph is a kal-graph.json
 *   frozen at build time, so there is nothing to re-read.
 */

import { closeMenu } from './menuFocus';

/* ── events ────────────────────────────────────────────────────────── */

export interface EventRef {
	off(): void;
}

const DEAD: EventRef = { off() {} };

export class Events {
	private handlers = new Map<string, Set<(...a: unknown[]) => void>>();
	on(name: string, cb: (...a: unknown[]) => void): EventRef {
		if (!this.handlers.has(name)) this.handlers.set(name, new Set());
		this.handlers.get(name)!.add(cb);
		return { off: () => this.handlers.get(name)?.delete(cb) };
	}
	off(name: string, cb: (...a: unknown[]) => void): void {
		this.handlers.get(name)?.delete(cb);
	}
	trigger(name: string, ...a: unknown[]): void {
		this.handlers.get(name)?.forEach((cb) => cb(...a));
	}
}

/* ── the Component lifecycle ───────────────────────────────────────── */

export class Component {
	private _children: Component[] = [];
	private _cleanups: (() => void)[] = [];
	private _loaded = false;

	load(): void {
		if (this._loaded) return;
		this._loaded = true;
		this.onload();
		for (const c of this._children) c.load();
	}
	onload(): void {}

	unload(): void {
		if (!this._loaded) return;
		this._loaded = false;
		for (const c of this._children.splice(0)) c.unload();
		for (const fn of this._cleanups.splice(0)) {
			try {
				fn();
			} catch {
				/* An exception during cleanup does not stop the rest of the cleanup */
			}
		}
		this.onunload();
	}
	onunload(): void {}

	addChild<T extends Component>(c: T): T {
		this._children.push(c);
		if (this._loaded) c.load();
		return c;
	}
	removeChild<T extends Component>(c: T): T {
		const i = this._children.indexOf(c);
		if (i >= 0) this._children.splice(i, 1);
		c.unload();
		return c;
	}
	register(cb: () => void): void {
		this._cleanups.push(cb);
	}
	registerEvent(ref: EventRef | null | undefined): void {
		if (ref) this._cleanups.push(() => ref.off());
	}
	registerDomEvent(el: EventTarget, type: string, cb: EventListener, opts?: AddEventListenerOptions): void {
		el.addEventListener(type, cb, opts);
		this._cleanups.push(() => el.removeEventListener(type, cb, opts));
	}
	registerInterval(id: number): number {
		this._cleanups.push(() => window.clearInterval(id));
		return id;
	}
}

/* ── file types ────────────────────────────────────────────────────── */

export abstract class TAbstractFile {
	path = '';
	name = '';
	parent: TFolder | null = null;
	vault!: Vault;
}

export class TFile extends TAbstractFile {
	basename = '';
	extension = '';
	stat = { ctime: 0, mtime: 0, size: 0 };
}

export class TFolder extends TAbstractFile {
	children: TAbstractFile[] = [];
	isRoot(): boolean {
		return this.path === '/' || this.path === '';
	}
}

/* ── the App surface ───────────────────────────────────────────────── */

export interface DataAdapter {
	exists(path: string): Promise<boolean>;
	read(path: string): Promise<string>;
	write(path: string, data: string): Promise<void>;
	mkdir(path: string): Promise<void>;
}

export interface Vault {
	configDir: string;
	adapter: DataAdapter;
	getFiles(): TFile[];
	getAbstractFileByPath(path: string): TAbstractFile | null;
	cachedRead(file: TFile): Promise<string>;
	// The overload order matters.  With rest left as unknown[] alone the callback arguments become
	// unknown, and GraphStore's `file.path` / `file instanceof TFile` all break.
	on(name: 'create' | 'modify' | 'delete', cb: (file: TAbstractFile) => unknown): EventRef;
	on(name: 'rename', cb: (file: TAbstractFile, oldPath: string) => unknown): EventRef;
	on(name: string, cb: (...a: never[]) => unknown): EventRef;
}

export interface CachedMetadata {
	tags?: { tag: string }[];
	frontmatter?: Record<string, unknown> & { tags?: string | string[] };
}

export interface MetadataCache {
	resolvedLinks: Record<string, Record<string, number>>;
	unresolvedLinks: Record<string, Record<string, number>>;
	getFileCache(file: TFile): CachedMetadata | null;
	on(name: 'resolved' | 'resolve', cb: () => unknown): EventRef;
	on(name: string, cb: (...a: never[]) => unknown): EventRef;
}

export interface Workspace {
	openLinkText(link: string, source: string, newLeaf?: boolean): void;
	on(name: 'css-change' | 'layout-change', cb: () => unknown): EventRef;
	on(name: string, cb: (...a: never[]) => unknown): EventRef;
	getLeavesOfType(type: string): unknown[];
	detachLeavesOfType(type: string): void;
}

export interface App {
	vault: Vault;
	metadataCache: MetadataCache;
	workspace: Workspace;
}

export const DEAD_REF = DEAD;

/* ── utilities ─────────────────────────────────────────────────────── */

export function normalizePath(p: string): string {
	return p.replace(/\\/g, '/').replace(/\/{2,}/g, '/').replace(/^\.\//, '').replace(/\/$/, '');
}

export interface Debouncer<A extends unknown[], R> {
	(...args: A): R | void;
	cancel(): this;
	run(): R | void;
}

export function debounce<A extends unknown[], R>(
	fn: (...args: A) => R,
	timeout = 0,
	resetTimer = false,
): Debouncer<A, R> {
	let timer: number | null = null;
	let pending: A | null = null;
	const wrapper = ((...args: A) => {
		pending = args;
		if (timer !== null) {
			if (!resetTimer) return;
			window.clearTimeout(timer);
		}
		timer = window.setTimeout(() => {
			timer = null;
			const a = pending;
			pending = null;
			if (a) fn(...a);
		}, timeout);
	}) as Debouncer<A, R>;
	wrapper.cancel = function () {
		if (timer !== null) window.clearTimeout(timer);
		timer = null;
		pending = null;
		return this;
	};
	wrapper.run = function () {
		if (timer !== null) window.clearTimeout(timer);
		timer = null;
		const a = pending;
		pending = null;
		return a ? fn(...a) : undefined;
	};
	return wrapper;
}

export function getAllTags(cache: CachedMetadata | null): string[] | null {
	if (!cache) return null;
	const out = new Set<string>();
	for (const t of cache.tags ?? []) out.add(t.tag);
	const fm = cache.frontmatter?.tags;
	for (const t of Array.isArray(fm) ? fm : typeof fm === 'string' ? [fm] : []) {
		out.add(t.startsWith('#') ? t : `#${t}`);
	}
	return [...out];
}

export function getLanguage(): string {
	return (navigator.language || 'en').toLowerCase();
}

export const moment = {
	locale(): string {
		return getLanguage();
	},
};

export const Platform = {
	isMobile: matchMedia('(pointer: coarse)').matches && matchMedia('(max-width: 900px)').matches,
	isPhone: matchMedia('(max-width: 600px)').matches,
	isTablet: false,
	isDesktop: !matchMedia('(pointer: coarse)').matches,
	isDesktopApp: false,
	isMobileApp: false,
	isMacOS: /mac/i.test(navigator.userAgent),
	isWin: /win/i.test(navigator.userAgent),
	isLinux: /linux/i.test(navigator.userAgent),
	isSafari: /^((?!chrome|android).)*safari/i.test(navigator.userAgent),
};

/* ── fuzzy search ──────────────────────────────────────────────────── */

export interface SearchResult {
	score: number;
	matches: [number, number][];
}

/**
 * A stand-in for Obsidian's prepareFuzzySearch.
 * The exact scoring formula is not public, so it is not reproduced — only the sense of ranking is matched:
 *   a contiguous match > a subsequence match, an earlier match > a later one, fewer gaps scores higher.
 */
export function prepareFuzzySearch(query: string): (text: string) => SearchResult | null {
	const q = query.toLowerCase().trim();
	return (text: string): SearchResult | null => {
		if (!q) return { score: 0, matches: [] };
		const s = (text || '').toLowerCase();
		const direct = s.indexOf(q);
		if (direct >= 0) {
			// A contiguous match.  The earlier it is caught and the shorter the text, the higher
			return { score: 10 - direct * 0.05 - s.length * 0.002, matches: [[direct, direct + q.length]] };
		}
		const matches: [number, number][] = [];
		let qi = 0;
		let gaps = 0;
		let last = -1;
		for (let i = 0; i < s.length && qi < q.length; i++) {
			if (s[i] !== q[qi]) continue;
			matches.push([i, i + 1]);
			if (last >= 0) gaps += i - last - 1;
			last = i;
			qi++;
		}
		if (qi < q.length) return null;
		return { score: -1 - gaps * 0.05, matches };
	};
}

/* ── Notice (the toast) ────────────────────────────────────────────── */

let noticeHost: HTMLElement | null = null;

export class Notice {
	noticeEl: HTMLElement;
	constructor(message: string | DocumentFragment, timeout = 4000) {
		if (!noticeHost) {
			noticeHost = document.createElement('div');
			noticeHost.className = 'gxw-notices';
			document.body.appendChild(noticeHost);
		}
		this.noticeEl = document.createElement('div');
		this.noticeEl.className = 'gxw-notice';
		if (typeof message === 'string') this.noticeEl.textContent = message;
		else this.noticeEl.appendChild(message);
		noticeHost.appendChild(this.noticeEl);
		this.noticeEl.addEventListener('click', () => this.hide());
		if (timeout > 0) window.setTimeout(() => this.hide(), timeout);
	}
	setMessage(message: string): this {
		this.noticeEl.textContent = message;
		return this;
	}
	hide(): void {
		this.noticeEl.remove();
	}
}

/* ── Menu (the right-click context menu) ───────────────────────────── */

export class MenuItem {
	el: HTMLButtonElement;
	private handler: (() => void) | null = null;
	private disabled = false;
	constructor(private menu: Menu) {
		/* As a div, not one item was picked up by assistive technology and Tab could not reach it.
		   As a button, Enter/Space activation and disabled exposure are all free.  Moving within the
		   menu is the arrows' job, so tabindex is -1 and the first item is focused on opening. */
		this.el = document.createElement('button');
		this.el.type = 'button';
		this.el.className = 'gxw-menu-item';
		this.el.setAttribute('role', 'menuitem');
		this.el.tabIndex = -1;
		this.el.addEventListener('click', () => {
			if (this.disabled) return;
			this.menu.hide();
			this.handler?.();
		});
	}
	setTitle(title: string | DocumentFragment): this {
		if (typeof title === 'string') this.el.textContent = title;
		else this.el.appendChild(title);
		return this;
	}
	/** Icons are omitted in the web build — pulling in the whole of lucide is not worth it */
	setIcon(_icon: string | null): this {
		return this;
	}
	setDisabled(v: boolean): this {
		this.disabled = v;
		this.el.disabled = v;
		this.el.classList.toggle('is-disabled', v);
		return this;
	}
	setChecked(v: boolean): this {
		this.el.classList.toggle('is-checked', v);
		return this;
	}
	onClick(cb: () => void): this {
		this.handler = cb;
		return this;
	}
}

export class Menu {
	private el: HTMLElement;
	private away: ((e: MouseEvent) => void) | null = null;
	private keys: ((e: KeyboardEvent) => void) | null = null;
	/** The control that opened the menu.  Where focus returns on closing, and where aria-expanded goes. */
	private invoker: HTMLElement | null = null;

	constructor() {
		this.el = document.createElement('div');
		this.el.className = 'gxw-menu';
		this.el.setAttribute('role', 'menu');
	}
	addItem(cb: (item: MenuItem) => void): this {
		const item = new MenuItem(this);
		cb(item);
		this.el.appendChild(item.el);
		return this;
	}
	addSeparator(): this {
		const sep = document.createElement('div');
		sep.className = 'gxw-menu-sep';
		this.el.appendChild(sep);
		return this;
	}
	showAtMouseEvent(evt: MouseEvent): this {
		// This is the only place the caller can be known.  currentTarget is empty once the handler exits, so it is grabbed now.
		const from = evt.currentTarget instanceof HTMLElement ? evt.currentTarget : null;
		return this.showAtPosition({ x: evt.clientX, y: evt.clientY }, from);
	}
	showAtPosition(pos: { x: number; y: number }, invoker?: HTMLElement | null): this {
		const active = document.activeElement;
		this.invoker = invoker ?? (active instanceof HTMLElement && active !== document.body ? active : null);
		this.invoker?.setAttribute('aria-haspopup', 'menu');
		this.invoker?.setAttribute('aria-expanded', 'true');
		document.body.appendChild(this.el);
		this.el.style.left = `${Math.min(pos.x, window.innerWidth - this.el.offsetWidth - 8)}px`;
		this.el.style.top = `${Math.min(pos.y, window.innerHeight - this.el.offsetHeight - 8)}px`;
		// Without moving focus, the menu opens and the keyboard is still outside it
		this.items()[0]?.focus();
		this.keys = (e) => this.onKey(e);
		// Received on capture — so the graph canvas does not grab the arrows or Esc first
		document.addEventListener('keydown', this.keys, true);
		// Registered on the next tick — registered now, the very click that opened the menu closes it
		window.setTimeout(() => {
			this.away = () => this.hide();
			document.addEventListener('click', this.away, { once: true });
			document.addEventListener('contextmenu', this.away, { once: true });
		}, 0);
		return this;
	}
	hide(): this {
		if (this.away) {
			document.removeEventListener('click', this.away);
			document.removeEventListener('contextmenu', this.away);
			this.away = null;
		}
		if (this.keys) {
			document.removeEventListener('keydown', this.keys, true);
			this.keys = null;
		}
		// Removal and returning focus are `closeMenu`'s job —— **the order is the point**, so it lives in there.
		// (Calling remove() first here and deciding afterwards, activeElement is already <body> and
		//  focus never comes back.  That really was the state.)
		const back = this.invoker;
		this.invoker = null;
		closeMenu(this.el, back, document.activeElement);
		return this;
	}

	/** What the arrows cycle through.  disabled cannot be activated or focused, so it is skipped. */
	private items(): HTMLButtonElement[] {
		return Array.from(this.el.querySelectorAll<HTMLButtonElement>('.gxw-menu-item:not(:disabled)'));
	}

	private onKey(e: KeyboardEvent): void {
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			this.hide();
			return;
		}
		// Tab means leaving the menu —— close, return to where it was invoked from, and let the browser take it
		if (e.key === 'Tab') {
			this.hide();
			return;
		}
		const items = this.items();
		if (!items.length) return;
		const at = items.indexOf(document.activeElement as HTMLButtonElement);
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			e.preventDefault();
			const step = e.key === 'ArrowDown' ? 1 : -1;
			items[at < 0 ? (step > 0 ? 0 : items.length - 1) : (at + step + items.length) % items.length]?.focus();
			return;
		}
		if (e.key === 'Home') {
			e.preventDefault();
			items[0]?.focus();
			return;
		}
		if (e.key === 'End') {
			e.preventDefault();
			items[items.length - 1]?.focus();
		}
	}
}

/* ── Modal / SuggestModal ──────────────────────────────────────────── */

export class Modal {
	containerEl: HTMLDialogElement;
	modalEl: HTMLElement;
	contentEl: HTMLElement;
	private escHandler: (e: KeyboardEvent) => void;

	constructor(public app: App) {
		/* One line of <dialog>.showModal() gives role=dialog, aria-modal, a focus trap,
		   an inert background and focus restoration on close.  With a div all five have to be
		   hand-written, and the inert background cannot even be imitated
		   (measured: in the div era, 62 background buttons were still tabbable with the modal open). */
		this.containerEl = document.createElement('dialog');
		this.containerEl.className = 'gxw-modal-container';
		const bg = document.createElement('div');
		bg.className = 'gxw-modal-bg';
		bg.addEventListener('click', () => this.close());
		this.containerEl.appendChild(bg);
		this.modalEl = document.createElement('div');
		this.modalEl.className = 'gxw-modal';
		this.containerEl.appendChild(this.modalEl);
		this.contentEl = document.createElement('div');
		this.contentEl.className = 'gxw-modal-content';
		this.modalEl.appendChild(this.contentEl);
		/* Esc is handled by the dialog itself, but that close request dies outright if
		   preventDefault() is called on the Escape keydown —— GraphController clears the selection
		   with Escape on the canvas and calls preventDefault() (GraphController.ts:1353).
		   Measured: a bare <dialog> on the same page closed on Esc, and attaching one preventDefault
		   listener stopped it.  So a last line of defence receives it on the capture phase. */
		this.escHandler = (e) => {
			if (e.key === 'Escape') this.close();
		};
		/* Closing with Esc does not go through close() and arrives only here — cleanup happens in one place */
		this.containerEl.addEventListener('close', () => {
			document.removeEventListener('keydown', this.escHandler, true);
			this.containerEl.remove();
			this.onClose();
		});
	}
	open(): void {
		document.body.appendChild(this.containerEl);
		this.containerEl.showModal();
		document.addEventListener('keydown', this.escHandler, true);
		this.onOpen();
	}
	close(): void {
		this.containerEl.close();
	}
	onOpen(): void {}
	onClose(): void {}
}

/**
 * The SuggestModal stand-in — the search button lands here.
 * The keyboard (↑↓/Enter/Esc) is matched too.  Without it, Search is a dead button.
 */
export abstract class SuggestModal<T> extends Modal {
	inputEl: HTMLInputElement;
	resultContainerEl: HTMLElement;
	private items: T[] = [];
	private cursor = 0;

	constructor(app: App) {
		super(app);
		this.modalEl.classList.add('gxw-suggest');
		this.inputEl = document.createElement('input');
		this.inputEl.type = 'text';
		this.inputEl.className = 'gxw-suggest-input';
		this.resultContainerEl = document.createElement('div');
		this.resultContainerEl.className = 'gxw-suggest-results';
		this.contentEl.appendChild(this.inputEl);
		this.contentEl.appendChild(this.resultContainerEl);

		this.inputEl.addEventListener('input', () => this.refresh());
		this.inputEl.addEventListener('keydown', (e) => {
			if (e.key === 'ArrowDown') {
				e.preventDefault();
				this.move(1);
			} else if (e.key === 'ArrowUp') {
				e.preventDefault();
				this.move(-1);
			} else if (e.key === 'Enter') {
				e.preventDefault();
				this.choose(this.cursor);
			}
		});
	}

	abstract getSuggestions(query: string): T[] | Promise<T[]>;
	abstract renderSuggestion(value: T, el: HTMLElement): void;
	abstract onChooseSuggestion(value: T, evt: MouseEvent | KeyboardEvent): void;

	setPlaceholder(text: string): void {
		this.inputEl.placeholder = text;
	}

	onOpen(): void {
		this.inputEl.focus();
		void this.refresh();
	}

	private async refresh(): Promise<void> {
		this.items = await this.getSuggestions(this.inputEl.value);
		this.cursor = 0;
		this.resultContainerEl.textContent = '';
		this.items.forEach((item, i) => {
			const row = document.createElement('div');
			row.className = 'gxw-suggest-item';
			this.renderSuggestion(item, row);
			row.addEventListener('click', () => this.choose(i));
			row.addEventListener('mouseenter', () => this.setCursor(i));
			this.resultContainerEl.appendChild(row);
		});
		this.setCursor(0);
	}

	private move(d: number): void {
		if (!this.items.length) return;
		this.setCursor((this.cursor + d + this.items.length) % this.items.length);
	}

	private setCursor(i: number): void {
		this.cursor = i;
		const rows = Array.from(this.resultContainerEl.children);
		rows.forEach((r, j) => r.classList.toggle('is-selected', j === i));
		rows[i]?.scrollIntoView({ block: 'nearest' });
	}

	private choose(i: number): void {
		const item = this.items[i];
		if (item === undefined) return;
		this.close();
		this.onChooseSuggestion(item, new MouseEvent('click'));
	}
}

/* ── what the web entry point does not use (minimal form, for type resolution) ─────── */

export class WorkspaceLeaf {}

export class ItemView extends Component {
	contentEl: HTMLElement = document.createElement('div');
	containerEl: HTMLElement = document.createElement('div');
	app!: App;
	constructor(public leaf: WorkspaceLeaf) {
		super();
	}
	getViewType(): string {
		return '';
	}
	getDisplayText(): string {
		return '';
	}
	getIcon(): string {
		return '';
	}
}

export class Plugin extends Component {
	app!: App;
	manifest: Record<string, unknown> = {};
	addRibbonIcon(): HTMLElement {
		return document.createElement('div');
	}
	addCommand(): void {}
	addSettingTab(): void {}
	registerView(): void {}
	async loadData(): Promise<unknown> {
		return null;
	}
	async saveData(_d: unknown): Promise<void> {}
}

export class PluginSettingTab {
	containerEl: HTMLElement = document.createElement('div');
	constructor(
		public app: App,
		public plugin: Plugin,
	) {}
	display(): void {}
	hide(): void {}
}

export class Setting {
	settingEl: HTMLElement;
	constructor(containerEl: HTMLElement) {
		this.settingEl = document.createElement('div');
		containerEl.appendChild(this.settingEl);
	}
	setName(): this {
		return this;
	}
	setDesc(): this {
		return this;
	}
	addText(): this {
		return this;
	}
	addToggle(): this {
		return this;
	}
	addSlider(): this {
		return this;
	}
	addDropdown(): this {
		return this;
	}
	addButton(): this {
		return this;
	}
}

export function setIcon(_el: HTMLElement, _icon: string): void {
	/* lucide not included — presetIcons.ts draws its own SVG, so real usage is 0 */
}
