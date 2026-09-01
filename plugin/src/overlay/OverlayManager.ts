import type { App } from 'obsidian';
import { TFile, getAllTags } from 'obsidian';
import type { GraphData, GraphNode } from '../types';
import type { AggregateRenderer } from '../render/AggregateRenderer';
import { getLang, t } from '../i18n';
import { isMarkdownFile } from '../data/graphFiles';
import type { KdbEntityMeta, KdbRelationMeta } from '../data/kdbGraph';



export interface OverlayCallbacks {
	openNote: (id: string) => void;
	focusNode: (index: number) => void;
	/** The relation depth on the card (first degree by default, with an unobtrusive switch to the second) */
	getSelectionDepth: () => 1 | 2 | 3 | 4;
	onSelectionDepth: (depth: 1 | 2 | 3 | 4) => void;
	/** In KDB mode, that node's entity detail.  null otherwise */
	getKdbEntity?: (index: number) => KdbEntityMeta | null;
	/** The relations attached to that entity (including the other node's name) */
	getKdbRelations?: (index: number) => { other: string; meta: KdbRelationMeta }[];
	/** Document path → its frontmatter (null when absent) */
	getFrontmatter?: (path: string) => Record<string, unknown> | null;
}

/**
 * The DOM overlay (NASA mode: labels and cards stay off the canvas).
 * A hard budget: 14 hubs + 1 hover + ≤20 neighbours + 1 card —— ≤36 projections per frame, negligible.
 */
export class OverlayManager {
	private root: HTMLElement;
	private hubEls: { index: number; el: HTMLElement }[] = [];
	private neighborEls: { index: number; el: HTMLElement }[] = [];
	private hoverEl: HTMLElement;
	private hoverIndex = -1;
	private groupEl: HTMLElement;
	private groupName: string | null = null;
	private card: HTMLElement;
	private cardIndex = -1;
	private cardCollapsed = false;
	private cardPos: { x: number; y: number } | null = null; // the position after a manual drag (null = follow the node)
	private data: GraphData = { nodes: [], links: [] };
	private graphRadius = 200;
	private snippetToken = 0;
	private hubBudget = 14;
	private neighborBudget = 20;
	private mobileCard = false;

	constructor(
		parent: HTMLElement,
		private app: App,
		private renderer: AggregateRenderer,
		private cb: OverlayCallbacks,
	) {
		this.root = parent.createDiv({ cls: 'gx-overlay' });
		this.hoverEl = this.root.createDiv({ cls: 'gx-label gx-label-hover' });
		this.hoverEl.hide();
		this.card = this.root.createDiv({ cls: 'gx-card' });
		this.card.hide();
		// The community hover description.  Unlike a node card it follows the cursor —— what is
		// being pointed at is an area of the screen, not one node, so there is no projection point to attach to.
		this.groupEl = this.root.createDiv({ cls: 'gx-ghover' });
		this.groupEl.hide();
	}

	/** The quality tier's budget; the card switches to bottom-drawer mode (mobile) */
	setBudgets(hub: number, neighbor: number, mobileCard: boolean): void {
		this.hubBudget = hub;
		this.neighborBudget = neighbor;
		this.mobileCard = mobileCard;
		this.setData(this.data, this.graphRadius);
	}

	setData(data: GraphData, graphRadius: number): void {
		this.data = data;
		this.graphRadius = graphRadius;
		for (const h of this.hubEls) h.el.remove();
		this.hubEls = [...data.nodes.entries()]
			.filter(([, n]) => !n.unresolved)
			.sort((a, b) => b[1].degree - a[1].degree)
			.slice(0, this.hubBudget)
			.map(([index, n]) => ({
				index,
				el: this.root.createDiv({ cls: 'gx-label gx-label-hub', text: n.name }),
			}));
		// The old indices go stale after a data rebuild, so state depending on them is cleared
		this.setHover(-1);
		this.setSelection(-1, new Set());
	}

	setHover(index: number): void {
		this.hoverIndex = index;
		if (index < 0) {
			this.hoverEl.hide();
			return;
		}
		const node = this.data.nodes[index];
		if (!node) return;
		this.hoverEl.setText(node.name);
		this.hoverEl.show();
	}

	/**
	 * Shows the community hover description beside the cursor.  A null info hides it.
	 *
	 * It shows **the same content** as the left panel's summary box, and exists separately because
	 * looking at the right of the graph while the description appears at the far left, the eye cannot follow.
	 */
	setGroupHover(
		info: { name: string; summary: string; members: string } | null,
		x: number, y: number, w: number,
	): void {
		if (!info) {
			if (this.groupName !== null) { this.groupName = null; this.groupEl.hide(); }
			return;
		}
		if (info.name !== this.groupName) {
			this.groupName = info.name;
			this.groupEl.empty();
			this.groupEl.createDiv({ cls: 'gx-ghover-h', text: info.name });
			if (info.summary) this.groupEl.createDiv({ cls: 'gx-ghover-s', text: info.summary });
			if (info.members) this.groupEl.createDiv({ cls: 'gx-ghover-m', text: info.members });
			this.groupEl.show();
		}
		// At the right edge it flips to the left of the cursor.  The card width matches the max-width in the CSS.
		const CARD = 260;
        const left = x + 18 + CARD > w ? x - CARD - 18 : x + 18;
		this.groupEl.style.transform = `translate3d(${left.toFixed(1)}px, ${(y + 14).toFixed(1)}px, 0)`;
	}

	/**
	 * Adaptive bottom padding (M4.1): measures the actual pixel overlap between .mobile-navbar and the canvas.
	 * Obsidian exposes no navbar height variable, and on a tablet, with settings hidden, or on an Android variant it may not exist ——
	 * measuring at runtime adapts to every shape: 0 with no navbar, and no stray whitespace.
	 */
	private refreshBottomInset(): void {
		let inset = 0;
		const navbar = activeDocument.querySelector('.mobile-navbar');
		if (navbar) {
			const nb = navbar.getBoundingClientRect();
			const ce = this.root.getBoundingClientRect();
			inset = Math.max(0, Math.round(ce.bottom - nb.top));
		}
		this.root.setCssProps({ '--gx-bottom-inset': `${inset}px` });
	}

	/** Selection: neighbour labels + the card; index<0 clears */
	setSelection(index: number, neighbors: Set<number>): void {
		for (const e of this.neighborEls) e.el.remove();
		this.neighborEls = [];
		if (index !== this.cardIndex) this.cardPos = null; // a new node selected → the card follows again; the same node (a depth change) keeps the dragged position
		this.cardIndex = index;
		if (index < 0) {
			this.card.hide();
			return;
		}
		const byDegree = [...neighbors]
			.filter((i) => i !== index)
			.sort((a, b) => (this.data.nodes[b]?.degree ?? 0) - (this.data.nodes[a]?.degree ?? 0))
			.slice(0, this.neighborBudget);
		this.neighborEls = byDegree.map((i) => ({
			index: i,
			el: this.root.createDiv({ cls: 'gx-label gx-label-neighbor', text: this.data.nodes[i]?.name ?? '' }),
		}));
		const node = this.data.nodes[index];
		if (node) {
			if (this.mobileCard) {
				this.refreshBottomInset();
				// Remove the inline transform left over from desktop positioning → the CSS bottom-drawer positioning takes over by selector specificity (no !important needed)
				this.card.style.removeProperty('transform');
			}
			this.buildCard(node, index);
		}
	}

	/**
	 * The KDB entity card.  What differs from a note card:
	 *   · There is no file —— an entity spans several documents.  So it lists **its sources**
	 *   · Each source's frontmatter folds open (a path alone does not say what it is)
	 *   · It lists the relations —— the edges are the substance of this graph
	 */
	private buildKdbBody(body: HTMLElement, kdb: KdbEntityMeta, index: number): void {
		// The description, relations and sources scroll **as one block**.
		// A hub entity has 12 relations + 12 sources, putting the card well past the screen (measured 1,500px+).
		// Splitting the scroll area gives nested scrolling, which is worse, so it is kept as one.
		// The header (name, type) and Focus/Related sit outside this area and are always visible.
		const scroll = body.createDiv({ cls: 'gx-kdb-scroll' });
		if (kdb.desc) scroll.createDiv({ cls: 'gx-card-snippet is-kdb', text: kdb.desc });

		const rels = this.cb.getKdbRelations?.(index) ?? [];
		if (rels.length > 0) {
			const sec = scroll.createDiv({ cls: 'gx-kdb-sec' });
			sec.createDiv({ cls: 'gx-kdb-sec-h', text: `Relations ${rels.length}` });
			for (const { other, meta } of rels.slice(0, 12)) {
				const row = sec.createDiv({ cls: 'gx-kdb-rel' });
				row.createSpan({ cls: 'gx-kdb-rel-to', text: other });
				if (meta.desc) row.createSpan({ cls: 'gx-kdb-rel-d', text: meta.desc.slice(0, 160) });
				// Relations have sources too.  They can differ from the entity's, so they are listed separately.
				if (meta.docs.length > 0) this.buildDocList(row, meta.docs, true);
			}
		}

		if (kdb.docs.length > 0) {
			const sec = scroll.createDiv({ cls: 'gx-kdb-sec' });
			sec.createDiv({ cls: 'gx-kdb-sec-h', text: `Source documents ${kdb.docs.length}` });
			this.buildDocList(sec, kdb.docs, false);
		}

		const actions = body.createDiv({ cls: 'gx-card-actions' });
		const focusBtn = actions.createEl('button', { text: t('card.focus') });
		focusBtn.addEventListener('click', () => this.cb.focusNode(index));

		this.buildDepthRow(body);
	}

	/** The list of document paths.  Clicking opens one; pressing ▸ unfolds its frontmatter. */
	private buildDocList(parent: HTMLElement, docs: string[], compact: boolean): void {
		const list = parent.createDiv({ cls: compact ? 'gx-kdb-docs is-compact' : 'gx-kdb-docs' });
		for (const path of docs.slice(0, compact ? 3 : 12)) {
			const row = list.createDiv({ cls: 'gx-kdb-doc' });
			const fm = this.cb.getFrontmatter?.(path) ?? null;
			const keys = fm ? Object.keys(fm).filter((k) => k !== 'position') : [];

			const head = row.createDiv({ cls: 'gx-kdb-doc-head' });
			const caret = head.createSpan({ cls: 'gx-kdb-caret', text: keys.length > 0 ? '▸' : '·' });
			const link = head.createSpan({ cls: 'gx-kdb-path', text: path });
			link.addEventListener('click', () => this.cb.openNote(path));

			if (keys.length === 0) continue;
			const fmEl = row.createDiv({ cls: 'gx-kdb-fm' });
			fmEl.hide();
			for (const k of keys.slice(0, 12)) {
				const kv = fmEl.createDiv({ cls: 'gx-kdb-fm-row' });
				kv.createSpan({ cls: 'gx-kdb-fm-k', text: k });
				kv.createSpan({ cls: 'gx-kdb-fm-v', text: fmtValue(fm?.[k]) });
			}
			head.addEventListener('click', (e) => {
				if (e.target === link) return; // a click on the path opens it
				const open = fmEl.isShown();
				if (open) fmEl.hide();
				else fmEl.show();
				caret.setText(open ? '▸' : '▾');
			});
		}
	}

	private buildDepthRow(body: HTMLElement): void {
		const depthRow = body.createDiv({ cls: 'gx-card-depth' });
		depthRow.createSpan({ cls: 'gx-card-depth-label', text: t('card.links') });
		const mini = depthRow.createDiv({ cls: 'gx-card-depth-mini' });
		const cur = this.cb.getSelectionDepth();
		for (const d of [1, 2, 3, 4] as const) {
			const b = mini.createEl('button', { text: String(d) });
			b.toggleClass('is-on', cur === d);
			b.addEventListener('click', () => this.cb.onSelectionDepth(d));
		}
	}

	private buildCard(node: GraphNode, index: number): void {
		this.card.empty();
		this.card.show();
		this.card.toggleClass('is-collapsed', this.cardCollapsed);

		// —— the header: the title + collapse (the drag handle, moving the whole card) ——
		const head = this.card.createDiv({ cls: 'gx-card-head' });
		head.createDiv({ cls: 'gx-card-title', text: node.name });
		const collapseBtn = head.createEl('button', { cls: 'gx-card-collapse', text: this.cardCollapsed ? '+' : '–' });
		collapseBtn.addEventListener('click', (e) => {
			e.stopPropagation();
			this.cardCollapsed = !this.cardCollapsed;
			this.card.toggleClass('is-collapsed', this.cardCollapsed);
			collapseBtn.setText(this.cardCollapsed ? '+' : '–');
		});
		if (!this.mobileCard) this.bindCardDrag(head);

		// —— the body (collapsible) ——
		const body = this.card.createDiv({ cls: 'gx-card-body' });
		// Check for KDB first — the subtitle wording differs
		const kdb = this.cb.getKdbEntity?.(index) ?? null;
		const meta = body.createDiv({ cls: 'gx-card-meta' });
		const dot = meta.createSpan({ cls: 'gx-card-dot' });
		dot.style.background = this.renderer.nodeColorHex(index);
		meta.createSpan({
			// An entity has no folder.  Its id is 'kdb:1234', so '/' is not found and it used to fall
			// through to 'Root folder' — the type and connection count go here instead.  That is this node's real identity.
			text: kdb
				? `${kdb.type} · ${node.degree} links`
				: node.tag
					? t('card.tag')
					: node.unresolved
						? t('card.unresolved')
						: node.id.includes('/')
							? node.id.slice(0, node.id.lastIndexOf('/'))
							: t('card.root'),
		});

		// ── KDB mode: the entity detail + the source document paths + frontmatter ──
		if (kdb) {
			this.buildKdbBody(body, kdb, index);
			return;
		}

		const file = node.unresolved || node.tag ? null : this.app.vault.getAbstractFileByPath(node.id);
		const tfile = file instanceof TFile ? file : null;
		const markdownFile = tfile && isMarkdownFile(tfile) ? tfile : null;

		if (markdownFile) {
			const cache = this.app.metadataCache.getFileCache(markdownFile);
			const tags = cache ? (getAllTags(cache) ?? []) : [];
			if (tags.length > 0) {
				const tagRow = body.createDiv({ cls: 'gx-card-tags' });
				for (const t of tags.slice(0, 5)) tagRow.createSpan({ cls: 'gx-card-tag', text: t });
			}
		}

		const stats = body.createDiv({ cls: 'gx-card-stats' });
		// Native Intl rather than obsidian's moment (the latter's loose types trip the no-unsafe-* warnings)
		const mdate = tfile
			? ` · ${new Date(tfile.stat.mtime).toLocaleDateString(getLang() === 'zh' ? 'zh-CN' : 'en', { year: 'numeric', month: 'short', day: 'numeric' })}`
			: '';
		// A tag node: shows "N notes" (= the number of notes carrying this tag) rather than backlinks/outlinks
		stats.setText(node.tag ? t('card.tagNotes', { n: node.degree }) : t('card.stats', { in: node.inDegree, out: node.outDegree }) + mdate);

		if (markdownFile) {
			const snippetEl = body.createDiv({ cls: 'gx-card-snippet', text: '…' });
			const token = ++this.snippetToken;
			void this.app.vault.cachedRead(markdownFile).then((text) => {
				if (token !== this.snippetToken) return; // the selection has changed; discard the stale result
				snippetEl.setText(stripMarkdown(text).slice(0, 120) || t('card.empty'));
			});
		}

		const actions = body.createDiv({ cls: 'gx-card-actions' });
		if (!node.unresolved && !node.tag) {
			const openBtn = actions.createEl('button', { text: t('card.open') });
			openBtn.addEventListener('click', () => this.cb.openNote(node.id));
		}
		const focusBtn = actions.createEl('button', { text: t('card.focus') });
		focusBtn.addEventListener('click', () => this.cb.focusNode(index));

		// The relation depth: low priority, an unobtrusive first/second-degree switch on the card's bottom line
		const depthRow = body.createDiv({ cls: 'gx-card-depth' });
		depthRow.createSpan({ cls: 'gx-card-depth-label', text: t('card.links') });
		const mini = depthRow.createDiv({ cls: 'gx-card-depth-mini' });
		const cur = this.cb.getSelectionDepth();
		const mkDepth = (depth: 1 | 2 | 3 | 4, label: string) => {
			const b = mini.createEl('button', { text: label });
			b.toggleClass('is-on', cur === depth);
			b.addEventListener('click', () => this.cb.onSelectionDepth(depth));
		};
		mkDepth(1, t('card.d1'));
		mkDepth(2, t('card.d2'));
		mkDepth(3, '3');
		mkDepth(4, '4');
	}

	/** The card is draggable: drag the header handle to set a manual position; update() then stops following the node */
	private bindCardDrag(handle: HTMLElement): void {
		handle.addEventListener('pointerdown', (e) => {
			if ((e.target as HTMLElement).closest('button')) return; // the collapse button does not start a drag
			e.preventDefault();
			handle.setPointerCapture(e.pointerId);
			const rootRect = this.root.getBoundingClientRect();
			const cardRect = this.card.getBoundingClientRect();
			const baseX = cardRect.left - rootRect.left;
			const baseY = cardRect.top - rootRect.top;
			const startX = e.clientX;
			const startY = e.clientY;
			const move = (ev: PointerEvent) => {
				this.cardPos = { x: baseX + (ev.clientX - startX), y: baseY + (ev.clientY - startY) };
				this.card.style.transform = `translate3d(${this.cardPos.x}px, ${this.cardPos.y}px, 0)`;
			};
			const up = (ev: PointerEvent) => {
				handle.releasePointerCapture(ev.pointerId);
				handle.removeEventListener('pointermove', move);
				handle.removeEventListener('pointerup', up);
			};
			handle.addEventListener('pointermove', move);
			handle.addEventListener('pointerup', up);
		});
	}

	/** Every frame: project every tracked node and position with translate3d (GPU compositing, no reflow) */
	update(w: number, h: number): void {
		const far = this.graphRadius * 2.6;
		const near = this.graphRadius * 1.2;
		for (const { index, el } of this.hubEls) {
			const p = this.renderer.projectNode(index, w, h);
			if (p.behind || p.x < 0 || p.x > w || p.y < 0 || p.y > h) {
				el.setCssProps({ opacity: '0' });
				continue;
			}
			const dist = this.renderer.cameraDistanceTo(index);
			const a = Math.min(Math.max((far - dist) / (far - near), 0), 1);
			el.style.opacity = a.toFixed(2);
			el.style.transform = `translate3d(${p.x.toFixed(1)}px, ${(p.y - 14).toFixed(1)}px, 0)`;
		}
		for (const { index, el } of this.neighborEls) {
			const p = this.renderer.projectNode(index, w, h);
			el.style.opacity = p.behind ? '0' : '0.85';
			if (!p.behind) el.style.transform = `translate3d(${p.x.toFixed(1)}px, ${(p.y - 12).toFixed(1)}px, 0)`;
		}
		if (this.hoverIndex >= 0) {
			const p = this.renderer.projectNode(this.hoverIndex, w, h);
			if (!p.behind) this.hoverEl.style.transform = `translate3d(${p.x.toFixed(1)}px, ${(p.y - 18).toFixed(1)}px, 0)`;
		}
		if (this.cardIndex >= 0 && !this.mobileCard && !this.cardPos) {
			// It follows the node's projection automatically only while it has not been dragged by hand
			const p = this.renderer.projectNode(this.cardIndex, w, h);
			if (!p.behind) {
				const flip = p.x + 296 > w;
				const x = flip ? p.x - 296 : p.x + 16;
				const y = Math.min(Math.max(p.y - 40, 12), Math.max(h - this.card.clientHeight - 12, 12));
				this.card.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0)`;
			}
		}
	}

	dispose(): void {
		this.root.remove();
		this.hubEls = [];
		this.neighborEls = [];
	}
}

function stripMarkdown(text: string): string {
	return text
		.replace(/^---\n[\s\S]*?\n---\n?/, '') // frontmatter
		.replace(/!?\[\[([^\]|]+)(\|[^\]]+)?\]\]/g, '$1')
		.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
		.replace(/[#*`>~_]|---/g, '')
		.replace(/\s+/g, ' ')
		.trim();
}


/** A frontmatter value on one line.  Arrays and objects are mixed in, so it cannot be printed as-is. */
function fmtValue(v: unknown): string {
	if (v === null || v === undefined) return '';
	if (Array.isArray(v)) return v.map((x) => String(x)).join(', ').slice(0, 120);
	if (typeof v === 'object') return JSON.stringify(v).slice(0, 120);
	return String(v).slice(0, 120);
}
