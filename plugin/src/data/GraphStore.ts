import type { App, TAbstractFile } from 'obsidian';
import { Component, TFile, TFolder, debounce, getAllTags } from 'obsidian';
import type { GraphData } from '../types';
import { buildGraph } from './buildGraph';
import { applyFilter, folderStats, isFilterActive, parseFilterQuery, type FilterQuery, type NoteFilter } from './noteFilter';
import { buildAdjacency } from './Adjacency';
import type { Adjacency } from './Adjacency';
import { seedPosition, seedRadius } from './seed';
import { readGhostEdges } from '../settings/ghostEdgeImport';
import type { GhostEdgeRecord } from '../settings/ghostEdgeImport';
import { isMarkdownFile, selectGraphFiles } from './graphFiles';
import { CanvasLinkCache, mergeResolvedLinks, parseCanvasFileLinks } from './canvasLinks';
import { boundedTagHubLimit } from './tagLens';
import { loadKdbGraph, type KdbGraph } from './kdbGraph';

/** A ghost edge (a Constellation pending suggestion): node indices + a strength.  It does not count towards degree, enter adjacency, or take part in the layout forces. */
export interface GhostLink {
	source: number;
	target: number;
	score: number;
}

/**
 * The only module that reads metadataCache.
 * It holds GraphData + the coordinate array; a rebuild keeps the old coordinates by id
 * (an identity-preserving merge), so the layout only needs a gentle re-heat and the galaxy does not explode.
 */
export class GraphStore extends Component {
	data: GraphData = { nodes: [], links: [] };
	/** x,y,z × nodes.length, written in place by the layout engine and read-only to the renderer */
	positions = new Float32Array(0);
	/** CSR adjacency: selection and the tour work in O(neighbourhood) rather than O(all edges); rebuilt with every data change */
	adjacency: Adjacency = { offset: new Int32Array(1), neighbor: new Int32Array(0), linkOf: new Int32Array(0) };

	/** Knowledge DB mode —— a node is an entity, not a page. */
	kdb: KdbGraph | null = null;
	private kdbMode = false;
	private kdbGroupBy: 'type' | 'community' = 'type';
	private kdbMinDegree = 1;

	private includeUnresolved = false;
	private includeOrphans = true;
	private includeTags = false;
	private showTagHubs = false;
	private tagHubLimit = 20;
	/** Note filtering (#11): folder show/hide (the legend, primary) + a text query (the escape hatch, secondary) */
	private filter: NoteFilter = { hiddenFolders: new Set(), query: [] };
	/**
	 * The legend data: top-level folder → note count, descending by count.  **Computed over every
	 * unfiltered file** —— otherwise switching one folder off would make the other chips' numbers jump.  It is also what the colour hues are allocated from.
	 */
	folders: { folder: string; count: number }[] = [];
	private nodeCap: number | null = null;
	private linkCap: number | null = null;
	private onChanged: (() => void) | null = null;

	/** Ghost edges: unconfirmed data does not move the galaxy —— a dashed line floating over the existing structure is itself the visual argument that "a link is missing here" */
	ghostLinks: GhostLink[] = [];
	private showGhostEdges = true;
	private ghostRaw: GhostEdgeRecord[] = [];
	private ghostKey = '';
	/** Core Obsidian 1.13 does not index Canvas metadata; cache file-card links separately. */
	private readonly canvasLinks = new CanvasLinkCache();
	private dirtyCanvasPaths = new Set<string>();
	private structuralCanvasPaths = new Set<string>();
	private active = false;

	constructor(private app: App) {
		super();
	}

	/** dataChanged fires once the debounced rebuild completes (the caller is responsible for the reheat + rebuilding the render buffers) */
	init(
		includeUnresolved: boolean,
		includeOrphans: boolean,
		includeTags: boolean,
		showTagHubs: boolean,
		tagHubLimit: number,
		filter: { hiddenFolders: string[]; query: string },
		onChanged: () => void,
	): void {
		this.includeUnresolved = includeUnresolved;
		this.includeOrphans = includeOrphans;
		this.includeTags = includeTags;
		this.showTagHubs = showTagHubs;
		this.tagHubLimit = boundedTagHubLimit(tagHubLimit);
		// Through the field rather than the setter: the setter triggers a rebuild of its own, and the caller is about to call rebuild(false) anyway
		this.filter = { hiddenFolders: new Set(filter.hiddenFolders), query: parseFilterQuery(filter.query) };
		this.onChanged = onChanged;
		this.active = true;
		const rebuildSoon = debounce(() => this.rebuild(true), 800, true);
		const refreshCanvasSoon = debounce(() => {
			const paths = [...this.dirtyCanvasPaths];
			const structuralPaths = new Set(this.structuralCanvasPaths);
			this.dirtyCanvasPaths.clear();
			this.structuralCanvasPaths.clear();
			void this.refreshCanvasFiles(paths, structuralPaths);
		}, 800, true);
		const markCanvasDirty = (path: string, structural = false) => {
			this.canvasLinks.mark(path);
			this.dirtyCanvasPaths.add(path);
			if (structural) this.structuralCanvasPaths.add(path);
			refreshCanvasSoon();
		};
		this.register(() => {
			this.active = false;
			rebuildSoon.cancel();
			refreshCanvasSoon.cancel();
			this.dirtyCanvasPaths.clear();
			this.structuralCanvasPaths.clear();
		});
		this.registerEvent(this.app.metadataCache.on('resolved', rebuildSoon));
		this.registerEvent(this.app.vault.on('create', (file) => {
			if (isCanvasFile(file)) markCanvasDirty(file.path, true);
			else if (file instanceof TFile && isMarkdownFile(file)) rebuildSoon();
		}));
		this.registerEvent(this.app.vault.on('modify', (file) => {
			if (isCanvasFile(file)) markCanvasDirty(file.path);
		}));
		this.registerEvent(this.app.vault.on('rename', (file, oldPath) => {
			if (file instanceof TFolder) {
				this.canvasLinks.renameTree(oldPath, file.path);
				const canvases = this.app.vault.getFiles().filter((child) => isCanvasFile(child) && isPathInside(child.path, file.path));
				if (canvases.length === 0) rebuildSoon();
				else canvases.forEach((child) => markCanvasDirty(child.path, true));
				return;
			}
			const wasCanvas = isCanvasPath(oldPath);
			const isCanvas = isCanvasFile(file);
			if (wasCanvas && isCanvas) {
				this.canvasLinks.renameFile(oldPath, file.path, true);
				markCanvasDirty(file.path, true);
			} else if (wasCanvas) {
				this.canvasLinks.remove(oldPath);
				rebuildSoon();
			} else if (isCanvas) {
				this.canvasLinks.renameFile(oldPath, file.path, false);
				markCanvasDirty(file.path, true);
			} else if (isMarkdownPath(oldPath) || (file instanceof TFile && isMarkdownFile(file))) {
				this.canvasLinks.renameFile(oldPath, file.path, false);
				rebuildSoon();
			}
		}));
		this.registerEvent(this.app.vault.on('delete', (file) => {
			if (file instanceof TFolder) this.canvasLinks.removeTree(file.path);
			else if (isCanvasFile(file) || isCanvasPath(file.path)) this.canvasLinks.remove(file.path);
			if (file instanceof TFolder || isCanvasPath(file.path) || isMarkdownPath(file.path)) rebuildSoon();
		}));
	}

	async ensureCacheReady(): Promise<void> {
		await this.loadCanvasLinks();
	}

	/** Initial O(total Canvas bytes) read, once before the first graph build. */
	private async loadCanvasLinks(): Promise<void> {
		const canvases = this.app.vault.getFiles().filter(isCanvasFile);
		for (let offset = 0; offset < canvases.length; offset += 8) {
			await Promise.all(canvases.slice(offset, offset + 8).map(async (file) => {
				const path = file.path;
				const revision = this.canvasLinks.capture(path);
				try {
					const parsed = parseCanvasFileLinks(await this.app.vault.cachedRead(file));
					if (this.active && parsed !== null) this.canvasLinks.apply(path, revision, parsed);
				} catch {
					// A later modify event retries; an initial failure simply contributes no Canvas edges.
				}
			}));
			if (!this.active) return;
		}
	}

	/** Debounced incremental refresh: reread only changed Canvas files, then rebuild once. */
	private async refreshCanvasFiles(paths: readonly string[], structuralPaths: ReadonlySet<string>): Promise<void> {
		if (paths.length === 0) return;
		let shouldRebuild = paths.some((path) => structuralPaths.has(path));
		for (const path of new Set(paths)) {
			const revision = this.canvasLinks.capture(path);
			const file = this.app.vault.getAbstractFileByPath(path);
			if (!(file instanceof TFile) || !isCanvasFile(file)) {
				shouldRebuild = this.canvasLinks.remove(path) || shouldRebuild;
				continue;
			}
			try {
				const parsed = parseCanvasFileLinks(await this.app.vault.cachedRead(file));
				if (!this.active) return;
				if (parsed === null) continue; // Obsidian may emit modify while JSON is mid-write.
				const applied = this.canvasLinks.apply(path, revision, parsed);
				// Even unchanged links need one rebuild so file size/date-derived data stays current.
				shouldRebuild = applied.accepted || shouldRebuild;
			} catch {
				// Preserve the last good cache on transient read errors.
			}
		}
		if (shouldRebuild && this.active) this.rebuild(true);
	}

	setIncludeUnresolved(v: boolean): void {
		if (v === this.includeUnresolved) return;
		this.includeUnresolved = v;
		this.rebuild(true);
	}

	getIncludeUnresolved(): boolean {
		return this.includeUnresolved;
	}

	/** The quality tier's node/link caps; a change rebuilds (keeping the coordinates) */
	setCaps(nodeCap: number | null, linkCap: number | null): void {
		if (nodeCap === this.nodeCap && linkCap === this.linkCap) return;
		this.nodeCap = nodeCap;
		this.linkCap = linkCap;
		this.rebuild(true);
	}

	setIncludeOrphans(v: boolean): void {
		if (v === this.includeOrphans) return;
		this.includeOrphans = v;
		this.rebuild(true);
	}

	setIncludeTags(v: boolean): void {
		if (v === this.includeTags) return;
		this.includeTags = v;
		this.rebuild(true);
	}

	/** showTags is the data master switch; while it is off, only the setting is recorded and no rebuild is wasted on invalid hubs. */
	setTagHubs(show: boolean, limit: number): void {
		const bounded = boundedTagHubLimit(limit);
		if (show === this.showTagHubs && bounded === this.tagHubLimit) return;
		this.showTagHubs = show;
		this.tagHubLimit = bounded;
		if (this.includeTags) this.rebuild(true);
	}

	/**
	 * The text query (#11's escape hatch).  The caller debounces —— every rebuild reruns the layout, so it must not fire per key.
	 * It compares the parsed term list rather than the raw string: `file:a` and `file:  a` mean the same thing and should not waste a rebuild.
	 */
	setFilterQuery(raw: string): void {
		const next = parseFilterQuery(raw);
		if (sameQuery(next, this.filter.query)) return;
		this.filter = { hiddenFolders: this.filter.hiddenFolders, query: next };
		// KDB is not caught by rebuild alone — its filter is at the stage that re-reads the JSON
		if (this.kdbMode) void this.reloadKdb();
		else this.rebuild(true);
	}

	/** Folder show/hide (#11's main interaction: a clickable legend).  One click rebuilds; no debounce needed */
	setHiddenFolders(hidden: readonly string[]): void {
		const next = new Set(hidden);
		const cur = this.filter.hiddenFolders;
		if (next.size === cur.size && [...next].every((f) => cur.has(f))) return;
		this.filter = { hiddenFolders: next, query: this.filter.query };
		if (this.kdbMode) void this.reloadKdb();
		else this.rebuild(true);
	}

	isFiltered(): boolean {
		return isFilterActive(this.filter);
	}

	setShowGhostEdges(v: boolean): void {
		if (v === this.showGhostEdges) return;
		this.showGhostEdges = v;
		this.resolveGhost();
		this.onChanged?.();
	}

	/** Path → the current node index; an edge with either end missing from the graph is dropped */
	private resolveGhost(): void {
		if (!this.showGhostEdges || this.ghostRaw.length === 0) {
			this.ghostLinks = [];
			return;
		}
		const idxById = new Map<string, number>();
		this.data.nodes.forEach((n, i) => idxById.set(n.id, i));
		const out: GhostLink[] = [];
		for (const r of this.ghostRaw) {
			const s = idxById.get(r.source);
			const t = idxById.get(r.target);
			if (s === undefined || t === undefined || s === t) continue;
			out.push({ source: s, target: t, score: r.state === 'deferred' ? r.score * 0.6 : r.score });
		}
		this.ghostLinks = out;
	}

	/** Asynchronously re-read the protocol file; unchanged content does not disturb the renderer (guarding against a rebuild→refresh loop) */
	async refreshGhost(): Promise<void> {
		const raw = (await readGhostEdges(this.app)) ?? [];
		const key = JSON.stringify(raw);
		if (key === this.ghostKey) return;
		this.ghostKey = key;
		this.ghostRaw = raw;
		this.resolveGhost();
		this.onChanged?.();
	}

	/** preservePositions=false is for the benchmark (a brand-new deterministic seed → a full cold layout) */
	/** The KDB mode toggle.  Loading the JSON is asynchronous, so it ends here and the rebuild happens in the callback. */
	async setKdbMode(on: boolean): Promise<void> {
		this.kdbMode = on;
		if (on && !this.kdb) {
			this.kdb = await loadKdbGraph(this.app, {
				nodeCap: this.nodeCap,
				linkCap: this.linkCap,
				minDegree: this.kdbMinDegree,
				hiddenTypes: this.filter.hiddenFolders,
				query: this.filter.query,
				groupBy: this.kdbGroupBy,
			});
		}
		this.rebuild(false);
	}

	isKdbMode(): boolean {
		return this.kdbMode && this.kdb !== null;
	}

	/** A degree-floor change → re-read the JSON and build the graph afresh (file I/O only, tens of ms) */
	async setKdbMinDegree(v: number): Promise<void> {
		const next = Math.max(1, Math.min(50, Math.round(v)));
		if (next === this.kdbMinDegree) return;
		this.kdbMinDegree = next;
		if (this.kdbMode) await this.reloadKdb();
	}

	/** Switching type ↔ community.  The group name space changes wholesale, so the existing filter is discarded. */
	async setKdbGroupBy(v: 'type' | 'community'): Promise<void> {
		if (v === this.kdbGroupBy) return;
		this.kdbGroupBy = v;
		// Going to community mode with 'concept' switched off, that name exists nowhere and the filter becomes a ghost
		this.filter = { hiddenFolders: new Set(), query: this.filter.query };
		if (this.kdbMode) await this.reloadKdb();
	}

	async reloadKdb(): Promise<void> {
		this.kdb = await loadKdbGraph(this.app, {
				nodeCap: this.nodeCap,
				linkCap: this.linkCap,
				minDegree: this.kdbMinDegree,
				hiddenTypes: this.filter.hiddenFolders,
				query: this.filter.query,
				groupBy: this.kdbGroupBy,
			});
		if (this.kdbMode) this.rebuild(false);
	}

	rebuild(preservePositions: boolean): void {
		if (this.kdbMode && this.kdb) {
			// The FILTER list by **entity type** rather than folder.  The type is already in the
			// node's folderTop, so the filter and colour-group logic needs no changes.
			// The counts use the typeCounts the loader gave — counted from the nodes left on screen,
			// a type switched off would disappear from the list and could never be switched back on.
			this.folders = this.kdb.typeCounts;
			this.applyData(this.kdb.data, preservePositions);
			return;
		}
		const withTags = this.includeTags; // with the master switch off the cache is not read, so chips, colours and hubs all cost nothing hidden
		const all = selectGraphFiles(this.app.vault.getFiles());
		this.folders = folderStats(all); // the legend: computed over everything, unaffected by the filter (or the chip numbers would jump around)
		// A TFile carries path/basename, so it structurally satisfies FilterableRecord —— filter
		// first, then map, so a filtered-out note pays neither for getFileCache nor for the object allocation
		const files = applyFilter(all, this.filter).map((f) => {
			const rec: { path: string; basename: string; size: number; tags?: string[] } = {
				path: f.path,
				basename: f.basename,
				size: f.stat.size,
			};
			if (withTags && isMarkdownFile(f)) {
				const cache = this.app.metadataCache.getFileCache(f);
				rec.tags = cache ? (getAllTags(cache) ?? []) : [];
			}
			return rec;
		});
		const resolvedLinks = mergeResolvedLinks(this.app.metadataCache.resolvedLinks, this.canvasLinks.asLinkTable());
		const next = buildGraph(files, resolvedLinks, this.app.metadataCache.unresolvedLinks, {
			includeUnresolved: this.includeUnresolved,
			includeOrphans: this.includeOrphans,
			includeTags: this.includeTags,
			includeTagHubs: this.includeTags && this.showTagHubs,
			tagHubLimit: this.tagHubLimit,
			nodeCap: this.nodeCap,
			linkCap: this.linkCap,
		});

		this.applyData(next, preservePositions);
	}

	/** A coordinate-preserving merge + rebuilding the derived data.  Shared by the note graph and the KDB graph. */
	private applyData(next: GraphData, preservePositions: boolean): void {
		const oldIndexById = new Map<string, number>();
		if (preservePositions) {
			this.data.nodes.forEach((n, i) => oldIndexById.set(n.id, i));
		}
		const oldPositions = this.positions;

		const radius = seedRadius(next.nodes.length);
		const positions = new Float32Array(next.nodes.length * 3);
		next.nodes.forEach((n, i) => {
			const oi = oldIndexById.get(n.id);
			if (oi !== undefined && oi * 3 + 2 < oldPositions.length) {
				positions[i * 3] = oldPositions[oi * 3] ?? 0;
				positions[i * 3 + 1] = oldPositions[oi * 3 + 1] ?? 0;
				positions[i * 3 + 2] = oldPositions[oi * 3 + 2] ?? 0;
			} else {
				const [x, y, z] = seedPosition(n.id, radius);
				positions[i * 3] = x;
				positions[i * 3 + 1] = y;
				positions[i * 3 + 2] = z;
			}
		});

		this.data = next;
		this.positions = positions;
		this.adjacency = buildAdjacency(next); // derived data, keyed on the new (possibly reordered) indices
		this.resolveGhost(); // ghost edges are re-resolved along with the index reordering (using the cached ghostRaw, synchronously)
		this.onChanged?.();
		void this.refreshGhost(); // and asynchronously re-read the file while there: accept a suggestion → a real link lands on disk → the dashes vanish naturally on the next rebuild
	}
}

function isCanvasPath(path: string): boolean {
	return path.toLowerCase().endsWith('.canvas');
}

function isMarkdownPath(path: string): boolean {
	return path.toLowerCase().endsWith('.md');
}

function isCanvasFile(file: TAbstractFile): file is TFile {
	return file instanceof TFile && file.extension.toLowerCase() === 'canvas';
}

function isPathInside(path: string, folderPath: string): boolean {
	const prefix = folderPath.endsWith('/') ? folderPath : `${folderPath}/`;
	return path.startsWith(prefix);
}

/** Term-list equivalence: two semantically identical inputs (`file:a` / `file:  a`) should not waste a rebuild */
function sameQuery(a: FilterQuery, b: FilterQuery): boolean {
	if (a.length !== b.length) return false;
	for (let i = 0; i < a.length; i++) {
		const x = a[i];
		const y = b[i];
		if (!x || !y || x.field !== y.field || x.value !== y.value || x.negate !== y.negate) return false;
	}
	return true;
}
