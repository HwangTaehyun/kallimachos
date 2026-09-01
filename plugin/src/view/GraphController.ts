import type { App } from 'obsidian';
import { groupAnchors } from '../layout/groupAnchors';
import { isCollapsedSpread, maxRadius, reseedDecision, shouldReframe } from '../layout/spread';
import { Notice, Platform, TFile, debounce } from 'obsidian';
import { Spherical, Vector3 } from 'three';
import type { BenchResult, GraphNode } from '../types';
import type { GalaxySettings } from '../settings';
import { DEFAULT_SETTINGS, toLayoutParams } from '../settings';
import { readGraphColorGroups } from '../settings/graphJsonImport';
import type { ColorTheme } from '../render/colorThemes';
import { COLOR_THEMES } from '../render/colorThemes';
import { GraphStore } from '../data/GraphStore';
import { OTHER_GROUP } from '../layout/groupIndex';
import { KDB_PREFIX } from '../data/kdbGraph';
import { neighborhood, shortestPath } from '../data/Adjacency';
import { seedRadius } from '../data/seed';
import type { LayoutEngine } from '../layout/LayoutEngine';
import { MainThreadForceLayout } from '../layout/MainThreadForceLayout';
import { WorkerForceLayout } from '../layout/WorkerForceLayout';
import { AggregateRenderer } from '../render/AggregateRenderer';
import { makeNodeColorFn, makeTagColorFn, fallbackColorFn, assignFolderHues, folderCoveredByGroups, setExplicitColors, type NodeColorFn } from '../render/palette';
import { DAYLIGHT, DEEP_SPACE } from '../render/presets';
import { CameraDirector } from '../interactions/CameraDirector';
import { TourDirector } from '../tour/TourDirector';
import { ControlPanel } from '../overlay/ControlPanel';
import type { StylePreset } from '../render/stylePresets';
import { STYLE_PRESETS } from '../render/stylePresets';
import { OverlayManager } from '../overlay/OverlayManager';
import { NodeSearchModal } from './SearchModal';
import { PromptModal } from './PromptModal';
import { resolveLang, setLang, t } from '../i18n';
import type { LangPref } from '../i18n';
import { collectFrames, observeLongTasks, writeBenchResult, sleep } from '../bench/bench';
import type { QualityTier } from '../quality/tiers';
import { TIERS } from '../quality/tiers';
import { elapsedFrameSeconds, frameDeltaSeconds, progress01, safeFrameSeconds } from '../timing/frameClock';
import { WindowFrameLoop } from '../timing/windowFrameLoop';
import { WindowVisibilityBinding } from '../timing/windowVisibility';
import { selectGraphFiles } from '../data/graphFiles';
import { resolveTagLens, toggleTagLens as nextTagLens, topTags } from '../data/tagLens';
import type { TopTag } from '../data/tagLens';

const WARM_CACHE_MIN_COVERAGE = 0.8;
const ESTABLISHING_MS = 3200;
// Graded dimming (the aDim target): selected/1st-degree = full, 2nd = a shell, the rest fade out.  A starting point, tunable by eye.
const SELECT_DIM = { self: 1, d1: 1, d2: 0.45, rest: 0.12 };

/** The hit radius for a community hover (screen px).  Far wider than a node hover (10px) ——
 *  what is being pointed at is a visible clump, not a single dot.  The basis for the value is
 *  the measured table in AggregateRenderer.pickGroupAt's comment. */
const GROUP_HOVER_PX = 64;
// Community cohesion strength.  Measured (300 ticks to full settling · kNN purity K=10):
//   0    0.643   ← the link forces alone already clump somewhat
//   0.06 0.657   too weak to notice
//   0.15 0.686   ← adopted.  Visible clumping without overturning the link layout
//   0.30 0.714   tighter, but inter-community relations become a tangle across the screen
const COMMUNITY_PULL = 0.15;

/**
 * The single assembly point: Store → Layout → Renderer → Director → Overlay → Panel.
 * Its own rAF loop: 1 tick per frame while the layout is hot; zero uploads once settled.
 */
export class GraphController {
	readonly store: GraphStore;
	private layout: LayoutEngine = new WorkerForceLayout();
	private renderer: AggregateRenderer | null = null;
	private director: CameraDirector | null = null;
	private overlay: OverlayManager | null = null;
	private panel: ControlPanel | null = null;
	private tour: TourDirector | null = null;

	private frameLoop: WindowFrameLoop | null = null;
	private paused = false;
	private benchMode = false;
	private benchRunning = false;
	private selected = -1;
	/** Computed once on a data rebuild; a chip click reads only this top 12, so activating a Lens does not re-sort every tag. */
	private topTagList: TopTag[] = [];
	private graphRadius = 200;
	/** Did it warm-start from the cache.  If warm, it does not re-seed even after the data arrives. */
	private warmStart = false;
	private wasSettled = false;
	/** Has it re-framed once at the real size after a cold start */
	private framedOnSettle = false;
	/** The graph radius when the camera was last placed.  For tracking framing during settling. */
	private framedRadius = 0;
	/** Recomputing the radius is expensive (8,000 nodes).  Not done every frame. */
	private reframeTick = 0;
	/** The community currently highlighted.  Stops recomputation while the mouse moves inside the same one */
	private hoveredGroup: string | null = null;
	/** The node ids on screen a moment ago.  Decides "has anything newly appeared this time" */
	private knownIds = new Set<string>();
	private restartLayoutAfterReveal = false;
	private shot: { elapsedMs: number; durMs: number; fromBloom: number } | null = null;
	private maskEl: HTMLElement | null = null;

	private hudFrames: number[] = [];
	private boundWin: Window | null = null; // the window the view is currently in (it can be moved to a popout — see issue #4)
	private visibilityBinding: WindowVisibilityBinding | null = null;
	private disposeFns: (() => void)[] = [];
	private saveSoon: () => void;
	/** A structural operation (save/delete/move/rename a preset) writes to disk at once, bypassing the 800ms debounce —— so saving and immediately quitting does not lose it */
	private saveNow: () => void;
	/** Filter-query debounce (#11): every application rebuilds the graph and re-heats the layout, so firing per keystroke kills a large library */
	private filterSoon: (q: string) => void;
	/** The hub-count slider fires input continuously; the O(N+tags) graph rebuild happens once, after the hand stops. */
	private tagHubsSoon: (() => void) & { cancel(): void };
	/** The node colour function currently in effect; the panel legend needs it for the real colours, so a copy is kept here rather than only handed to the renderer */
	private colorFn: NodeColorFn = fallbackColorFn;
	private tier: QualityTier = TIERS.high;
	private autoLow = false; // is the auto tier currently down at low (it can go both ways)
	private lowFpsChecks = 0;
	private highFpsChecks = 0;
	private lastWatchdogAt = 0;
	private disposed = false;
	/** Rebuilt by the view when the WebGL context is lost (injected by GalaxyView) */
	onContextLost: (() => void) | null = null;

	constructor(
		private app: App,
		private contentEl: HTMLElement,
		private settings: GalaxySettings,
		saveSettings: () => void,
	) {
		this.store = new GraphStore(app);
		this.saveNow = saveSettings;
		this.saveSoon = debounce(saveSettings, 800, true);
		// 300ms: short enough that the result lands as you stop typing, long enough that continuous typing does not rebuild per key (on a 3.2k-note library one rebuild + re-heat starts at tens of ms)
		this.filterSoon = debounce((q: string) => this.store.setFilterQuery(q), 300, false);
		this.tagHubsSoon = debounce(
			() => this.store.setTagHubs(this.settings.showTagHubs, this.settings.tagHubLimit),
			180,
			false,
		);
	}

	get counts(): { nodes: number; links: number } {
		return { nodes: this.store.data.nodes.length, links: this.store.data.links.length };
	}

	async start(): Promise<void> {
		this.store.init(
			this.settings.showUnresolved,
			this.settings.showOrphans,
			this.settings.showTags,
			this.settings.showTagHubs,
			this.settings.tagHubLimit,
			{ hiddenFolders: this.settings.hiddenFolders, query: this.settings.filterQuery },
			() => this.onDataChanged(),
		);
		await this.store.ensureCacheReady();
		if (this.disposed) return;

		// Apply the saved KDB settings **at startup**.
		// Without this, leaving entity mode on and restarting brings up the note-link graph —
		// settings say kdbMode=true while the store does not know it.
		// (minDegree goes in first because the mode is still off, so only the field is set,
		//  and the setKdbMode that follows reads it in one go.  No double load.)
		// groupBy first — setKdbGroupBy empties the filter on a switch.  The other order
		// throws away the user's saved hiddenFolders the moment it starts.
		await this.store.setKdbGroupBy(this.settings.kdbGroupBy);
		await this.store.setKdbMinDegree(this.settings.kdbMinDegree);
		if (this.settings.kdbMode) await this.store.setKdbMode(true);
		else this.store.rebuild(false);
		// The saved filters are split per namespace.  Only the one matching the current mode is taken out.
		this.loadNamespacedFilter();
		if (this.disposed) return;

		// Warm start: overwrite the seed with the last settled coordinates → reopening is already formed
		const coverage = this.applyPositionCache();
		const warm = coverage >= WARM_CACHE_MIN_COVERAGE;

		const container = this.contentEl.createDiv({ cls: 'galaxy-view-canvas' });
		this.graphRadius = seedRadius(this.store.data.nodes.length) * 1.6;
		const renderer = new AggregateRenderer(container, this.graphRadius);
		this.renderer = renderer;
		this.applyColorFn();
		renderer.setData(this.store.data, this.store.positions);
		renderer.setGhostLinks(this.settings.showGhostEdges ? this.store.ghostLinks : []);
		this.warmStart = warm;
		// On a cold start, separate the seeds by community (see the measurements in seedByGroup's comment)
		//
		// ⚠ Nothing happens here on the web —— the graph arrives **asynchronously** over
		//   /api/graph, so store.data.nodes is empty at this point.  The real seeding happens
		//   in onDataChanged() once the data arrives.  See seededOnce below.
		if (!warm) this.seedByGroup();
		this.initLayout(warm ? 0.06 : 1);
		// A warm start is placed at real coordinates by playEstablishing.  Only a cold one needs re-framing after settling.
		this.framedOnSettle = warm;

		this.director = new CameraDirector(renderer.camera, renderer.renderer.domElement, {
			onFlyToSelected: () => this.flyToSelected(),
			onResetView: () => this.recenter(),
		});
		// The default framing elevation for a disc (the galaxy rework looks down on the disc by default)
		const galaxyElev = STYLE_PRESETS.find((p) => p.id === 'galaxy')?.frameElevDeg;
		if (galaxyElev !== undefined) this.director.setFramingElev(galaxyElev);

		this.overlay = new OverlayManager(this.contentEl, this.app, renderer, {
			openNote: (id) => this.openDoc(id),
			focusNode: (i) => this.selectNode(i, true),
			getSelectionDepth: () => this.settings.selectionDepth,
			onSelectionDepth: (depth) => {
				this.settings.selectionDepth = depth;
				this.saveSoon();
				if (this.selected >= 0) this.selectNode(this.selected, false);
			},
			getKdbEntity: (i) => (this.store.isKdbMode() ? (this.store.kdb?.meta[i] ?? null) : null),
			getKdbRelations: (i) => {
				const kdb = this.store.kdb;
				if (!this.store.isKdbMode() || !kdb) return [];
				const out: { other: string; meta: (typeof kdb.linkMeta)[number] }[] = [];
				for (const lm of kdb.linkMeta) {
					if (lm.source !== i && lm.target !== i) continue;
					const o = lm.source === i ? lm.target : lm.source;
					const name = kdb.data.nodes[o]?.name;
					if (name) out.push({ other: name, meta: lm });
				}
				// Highest degree first —— what matters survives even when the card is cut off
				out.sort((a, b) => {
					const da = kdb.data.nodes[a.meta.source === i ? a.meta.target : a.meta.source]?.degree ?? 0;
					const db = kdb.data.nodes[b.meta.source === i ? b.meta.target : b.meta.source]?.degree ?? 0;
					return db - da;
				});
				return out;
			},
			getFrontmatter: (path) => {
				const f = this.app.vault.getAbstractFileByPath(path);
				if (!(f instanceof TFile)) return null;
				const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
				return fm ? (fm as Record<string, unknown>) : null;
			},
		});
		this.overlay.setData(this.store.data, this.graphRadius);

		this.tour = new TourDirector({
			nodeCount: () => this.store.data.nodes.length,
			degreeOf: (i) => (this.store.data.nodes[i]?.tag ? 0 : (this.store.data.nodes[i]?.degree ?? 0)),
			nodePosition: (i, out) => renderer.nodePosition(i, out),
			graphRadius: () => this.graphRadius,
			selectNode: (i, fly) => this.selectNode(i, fly),
			clearSelection: () => this.clearSelection(),
			recenter: () => this.recenter(),
			flyPath: (wp, dur, opts) => this.director?.flyPath(wp, dur, opts),
			beginIdleOrbit: () => this.director?.beginFocusOrbit(null),
			onPath: () => this.director?.onPath ?? false,
			onStateChange: (on) => {
				this.panel?.setTourRunning(on);
				if (this.director) this.director.tourActive = on; // force orbiting during a tour (even with cruise off)
			},
		});

		this.applySettings();
		this.applyPreset();
		this.applyTier();
		this.buildPanel();
		this.applyTagLens();
		this.bindPicking(renderer.renderer.domElement);
		this.bindContextLost(renderer.renderer.domElement);
		this.bindVisibility();
		this.resize();

		// Import the 2D palette on first use (only if it has never been imported)
		if (this.settings.colorGroups.length === 0) void this.importColors(false);

		// Opening: a warm start gets the "pull-out" opening shot; a cold start watches the galaxy form (theatre in itself)
		if (warm) this.playEstablishing();
		else {
			const f = this.computeFraming();
			this.director.setInitialFraming(f.center, f.radius);
		}

		const loop = (now: number, previousNow: number | null) => {
			if (this.disposed) return;
			// The rAF timestamp of different Electron windows is not guaranteed to share a timeOrigin.
			// The first frame after a window switch takes 0; animation uses the untruncated safe elapsed
			// to keep the real duration, while simulation and rendering still cap long frames to avoid a jump (#14).
			const animationDeltaS = elapsedFrameSeconds(now, previousNow);
			const deltaS = frameDeltaSeconds(now, previousNow);
			if (!this.paused) {
				// If the Worker died asynchronously (file:// and the like), switch to the main thread here.
				// initLayout's try/catch only catches synchronous exceptions.
				if (this.layout instanceof WorkerForceLayout && this.layout.dead) this.fallbackToMainThread();
				if (!this.renderer?.revealing && this.layout.step()) this.renderer?.updatePositions();
				if (!this.restartLayoutAfterReveal) this.checkSettled();
				if (!this.benchMode) {
					// tick + the camera update share one safety net: neither may let an exception bubble up and freeze the whole rAF loop
					// (the flyby's CatmullRom sampling once threw inside director.update and froze the entire view = "I clicked and nothing happened")
					try {
						this.tour?.tick(animationDeltaS); // the tour choreographs this frame's motion first, then the director executes it; frozen along with paused
						this.director?.update(now, animationDeltaS, deltaS);
					} catch (err) {
						this.tour?.abort();
						this.director?.cancelMotion(); // clear a possibly damaged path or tween so the next frame does not throw again
						new Notice(t('notice.tourError', { msg: err instanceof Error ? err.message : String(err) }));
					}
				}
				this.stepShot(animationDeltaS);
				this.renderer?.render(deltaS, animationDeltaS);
				if (this.restartLayoutAfterReveal && !this.renderer?.revealing) {
					this.restartLayoutAfterReveal = false;
					this.initLayout(0.06);
					this.wasSettled = false;
				}
				const { clientWidth: w, clientHeight: h } = this.contentEl;
				this.overlay?.update(w, h);
			}
			this.updateHud(now);
			this.watchdog(now);
		};
		this.frameLoop = new WindowFrameLoop(loop);
		this.frameLoop.setOwner(this.boundWin ?? this.contentEl.ownerDocument.defaultView ?? window);
	}

	/** An Electron GPU reset → a mask + one-click rebuild (one of the hidden causes of death in the predecessor plugin) */
	private bindContextLost(canvas: HTMLElement): void {
		const onLost = (e: Event) => {
			e.preventDefault();
			const mask = this.contentEl.createDiv({ cls: 'gx-mask' });
			const btn = mask.createEl('button', { cls: 'gx-mask-btn', text: t('mask.contextLost') });
			btn.addEventListener('click', () => this.onContextLost?.());
		};
		canvas.addEventListener('webglcontextlost', onLost);
		this.disposeFns.push(() => canvas.removeEventListener('webglcontextlost', onLost));
	}

	resize(): void {
		// The view can be "moved to a new window" —— a changed document means rebinding visibility (otherwise the old observer wrongly reads it as hidden → a black screen, issue #4)
		if (this.contentEl.ownerDocument.defaultView !== this.boundWin) this.rebindVisibility();
		const { clientWidth: w, clientHeight: h } = this.contentEl;
		this.renderer?.resize(w, h);
	}

	// ---------- warm start and the opening shot ----------

	/**
	 * A signature saying **what layout the coordinate cache was made with**.
	 *
	 * What happened without it —— even after community pull was turned on, the first screen was
	 * the same old single clump.  A warm start overwrites with the cached coordinates and begins
	 * at alpha 0.06, and those coordinates come from a time with no cohesion, so they barely move.
	 * Only pressing any preset, which re-runs the layout, finally separated the communities.
	 * (user report 2026-08-19)
	 *
	 * Only what changes the **shape** of the layout goes in.  Colour and bloom are unrelated to
	 * coordinates, and the physics sliders apply live to a running simulation, so there is no
	 * reason to throw the cache away for them.
	 */
	private layoutCacheKey(): string {
		const mode = this.store.isKdbMode() ? this.settings.kdbGroupBy : 'notes';
		// The seed version goes in.  **A changed seeding scheme has to discard the old coordinates too** ——
		// with only the parameters in the key, a screen that already had a cache kept the same key, warm-started,
		// and seedByGroup never ran at all.  Measured: separation 0.056 in the cached state,
		// 6.838 with only the seeding applied to the same data.  That is why a real fix looked like no fix.
		// Change the seeding algorithm again and this number goes up.
		return `${mode}|c=${this.layoutParams().community}|s4`;
	}

	/**
	 * Separates the seed coordinates by community.  Called **only on a cold start and a group switch**.
	 *
	 * Why it is needed —— forceCommunity is only a contracting force pulling a community towards
	 * **that community's current centre of mass**; it does not push communities apart
	 * (galaxyForces.ts:83).  So starting from a mixed seed sphere leaves every centre of mass
	 * overlapping at the origin, each merely contracting.
	 *
	 * Measured (3,319 entities · 12 communities):
	 *     cold start as-is        intra-community radius 87 · inter-community distance   5 · separation 0.056
	 *     re-heated from separated  intra 557 · inter 965 · separation 1.731
	 * The forces are identical and only **the starting position** differs.  So the fix is the seed, not the force.
	 *
	 * The anchors are spread evenly on a Fibonacci sphere.  They do not fight the link forces because
	 * this is **an initial condition**, not a force acting every tick —— the links pull a community
	 * across, and it drifts that way of its own accord.
	 */
	/** Are the coordinates collapsed into one clump.  1.5× graphRadius is the line between collapsed and spread.
	 *  (measured: collapsed 119 · graphRadius 255 · just after seeding 1081 · after settling 1272) */
	private isCollapsed(): boolean {
		//  The verdict is owned by `layout/spread.ts` —— the tests live there.
		return isCollapsedSpread(this.store.positions, this.store.data.nodes.length, this.graphRadius);
	}

	private seedByGroup(): void {
		const nodes = this.store.data.nodes;
		if (nodes.length === 0) return;
		const anchorOf = new Map<string, number>();
		for (const n of nodes) {
			const g = n.folderTop;
			if (!g || g === OTHER_GROUP) continue;   // 'other' is not one clump
			if (!anchorOf.has(g)) anchorOf.set(g, anchorOf.size);
		}
		const k = anchorOf.size;
		if (k < 2) return;

		// Put the seeds **well outside the collapse radius**.
		//
		// Why 1.4× is not enough —— this layout has two stable states.  The galaxy preset's
		// centerPull (0.03) and coreGravity (−0.08) draw the graph into a 'collapsed' ball of
		// radius 119, and there is a 'spread' state at radius 1,300–1,700.
		// 1.4× (≈357) is inside the collapse radius, so gravity simply swallowed the seeds.
		//
		// Measured (3,319 entities · 12 communities, 2026-08-19):
		//     seed 1.4× → settled    radius  119 · separation 0.056   ← does not separate
		//     with gravity at 0      radius 1730 · separation 3.164   (to confirm the cause)
		//     seed 4×   → settled    radius 1358 · separation 2.603   ← adopted
		// The physics settings are the user's choice, so only the starting position changes.
		//  The anchor computation lives in `groupAnchors` alone.  It is used only here —— a
		//  **pulling force** towards this spot was once added here and removed.  Why is in tests/typeClustering.test.ts.
		const A = groupAnchors(k, this.anchorRadius());

		// Deterministic jitter.  Math.random would make the picture differ on every reload,
		// which becomes "why is it different every time".
		const jit = (i: number, salt: number) => {
			const h = Math.sin(i * 127.1 + salt * 311.7) * 43758.5453;
			return (h - Math.floor(h) - 0.5) * this.graphRadius * 0.35;
		};
		const P = this.store.positions;
		for (let i = 0; i < nodes.length; i++) {
			const a = anchorOf.get(nodes[i]?.folderTop ?? '');
			if (a === undefined) continue;            // 'other' stays scattered where it was
			P[i * 3] = (A[a * 3] ?? 0) + jit(i, 1);
			P[i * 3 + 1] = (A[a * 3 + 1] ?? 0) + jit(i, 2);
			P[i * 3 + 2] = (A[a * 3 + 2] ?? 0) + jit(i, 3);
		}
	}

	private applyPositionCache(): number {
		const cache = this.settings.positionCache;
		const nodes = this.store.data.nodes;
		if (nodes.length === 0) return 0;
		if (this.settings.positionCacheKey !== this.layoutCacheKey()) {
			// Coordinates made with a different layout.  Using them freezes the old shape —— go cold.
			return 0;
		}
		let hits = 0;
		nodes.forEach((n, i) => {
			const p = cache[n.id];
			if (!p) return;
			this.store.positions[i * 3] = p[0];
			this.store.positions[i * 3 + 1] = p[1];
			this.store.positions[i * 3 + 2] = p[2];
			hits++;
		});

		//  ★ **How many coordinates there are is not the only question —— whether they are usable matters too.**
		//
		//    This layout has two stable states (see seedByGroup's comment): 'collapsed' with
		//    gravity having drawn everything in (radius ~120), and 'spread' (radius 1,300–1,700).
		//    Once a cache is saved in the collapsed state, **a warm start from then on restores it
		//    exactly and declares "settling complete" without running a single tick.**  Clumped forever.
		//
		//    Measured (2026-08-23, 7,974 entities):
		//        straight after a reload   max radius 160 · 0 ticks · warm start true   ← clumped
		//        changing a preset re-heats it and it spreads to radius 1,300+
		//    "Why is it clumped at first and only spreads when I press a preset" is exactly this.
		//
		//    The seed radius is `graphRadius * 4`, so a spread layout is far beyond graphRadius.
		//    Anything smaller is collapsed coordinates —— discard them and go cold.
		//    The verdict uses `isCollapsed()` **as-is**.  The coordinates were just filled in, so
		//    it looks at the same array.  A separate criterion would split the two, producing a
		//    state where one says collapsed and the other says not.
		if (hits > 0 && this.isCollapsed()) {
			return 0;
		}
		return hits / nodes.length;
	}

	private checkSettled(): void {
		const settled = this.layout.isSettled();

		//  The camera follows the graph **during** settling too.
		//  It used to be placed once, when settling finished, and for those 20 seconds the camera
		//  sat inside the graph, a screen buried among the stars (measured: at 6 seconds, camera 482 · radius 1,519).
		//  It re-places only as much as the graph grew, so it does not shake (shouldReframe in spread.ts).
		if (!settled && !this.framedOnSettle && ++this.reframeTick % 30 === 0) {
			const r = maxRadius(this.store.positions, this.store.data.nodes.length);
			if (
				shouldReframe({ radius: r, framedRadius: this.framedRadius })
				&& this.selected < 0 && this.director && !this.director.userMoved
			) {
				this.framedRadius = r;
				this.recenter();
			}
		}

		if (settled && !this.wasSettled) {
			// A cold start places the initial framing at the **seed radius**, and the forces spread
			// the graph far wider than that (measured 255 → 3,500).  Without re-placing, the camera
			// stays inside the graph and starts 'buried among the stars'.  Corrected once, at settling.
			if (!this.framedOnSettle) {
				this.framedOnSettle = true;
				this.framedRadius = maxRadius(this.store.positions, this.store.data.nodes.length);
				if (this.selected < 0 && this.director && !this.director.userMoved) this.recenter();
			}
			this.renderer?.refreshClusterClouds(); // the cluster clouds are recomputed on the settled coordinates
			// The settling moment: write the warm-start cache (coordinates rounded to 1 decimal, to keep data.json small)
			const cache: Record<string, [number, number, number]> = {};
			const pos = this.store.positions;
			this.store.data.nodes.forEach((n, i) => {
				cache[n.id] = [
					Math.round((pos[i * 3] ?? 0) * 10) / 10,
					Math.round((pos[i * 3 + 1] ?? 0) * 10) / 10,
					Math.round((pos[i * 3 + 2] ?? 0) * 10) / 10,
				];
			});
			this.settings.positionCache = cache;
			this.settings.positionCacheKey = this.layoutCacheKey();
			this.saveSoon();
		}
		this.wasSettled = settled;
	}

	private playEstablishing(): void {
		const renderer = this.renderer;
		const director = this.director;
		if (!renderer || !director) return;
		this.maskEl = this.contentEl.createDiv({ cls: 'gx-mask' });
		this.maskEl.createDiv({ cls: 'gx-mask-text', text: t('mask.building') });
		// Wait a few frames for the first render to be ready, then unveil and pull out
		window.setTimeout(() => {
			if (!this.maskEl) return;
			this.maskEl.addClass('is-fading');
			window.setTimeout(() => {
				this.maskEl?.remove();
				this.maskEl = null;
			}, 650);
			const f = this.computeFraming();
			const inner = f.radius * 0.5;
			const elev = (10 * Math.PI) / 180;
			// The opening takes off from inside the centre of mass and pulls out to the global framing
			renderer.camera.position.set(f.center.x + inner * Math.cos(elev), f.center.y + inner * Math.sin(elev), f.center.z + inner * 0.2);
			director.target.copy(f.center);
			director.resetView(f.center, f.radius, () => director.beginFocusOrbit(null)); // inside → overview → cruise immediately
			// If the warm layout is still computing, the Worker really has to be stopped; skipping layout.step alone
			// cannot stop it changing positions in the background, which would make the reveal's last frame suddenly
			// catch up on 2.6s of accumulated displacement.  Afterwards it resumes cold from the same coordinates.
			if (!this.layout.isSettled()) {
				this.layout.dispose();
				this.restartLayoutAfterReveal = true;
			}
			renderer.playReveal(2600); // the creation animation: nodes bloom outwards from the centre in waves (G2.5 feedback)
			this.frameLoop?.resetClock(); // precompilation time does not count towards the reveal's progress
			this.shot = { elapsedMs: 0, durMs: ESTABLISHING_MS, fromBloom: this.settings.bloom.strength * 1.8 };
		}, 450);
	}

	/** During the opening, the glow falls from 1.8× back to the configured value (NASA's "bright birth") */
	private stepShot(deltaS: number): void {
		if (!this.shot || !this.renderer) return;
		this.shot.elapsedMs += safeFrameSeconds(deltaS) * 1000;
		const t = progress01(this.shot.elapsedMs, this.shot.durMs);
		const v = this.shot.fromBloom + (this.settings.bloom.strength - this.shot.fromBloom) * t;
		this.renderer.setBloomStrength(v);
		if (t >= 1) this.shot = null;
	}

	// ---------- data ----------

	/**
	 * The single colouring entry point: first hand out fallback hues in legend order (fixing collisions), then
	 * pass them to the renderer, keeping a copy for the panel legend to read the real colours from.
	 * With no fn given it picks automatically from the current colorGroups (groups if there are any, the folder fallback otherwise).
	 */
	private applyColorFn(fn?: NodeColorFn): void {
		const groups = this.settings.colorGroups;
		// Hues are handed out ranked by note count, and only to folders no colour group has taken —— all of it before colorFn takes effect
		// In KDB mode the colours the data brought are planted first.  Without it, 21 communities
		// cycle through HUES's 9 slots and different communities look the same colour.
		setExplicitColors(this.store.isKdbMode() ? (this.store.kdb?.groupColors ?? new Map()) : new Map());
		assignFolderHues(
			this.store.folders.map((f) => f.folder),
			(folder) => folderCoveredByGroups(folder, groups),
		);
		const base = fn ?? (groups.length > 0 ? makeNodeColorFn(groups) : fallbackColorFn);
		this.colorFn = this.settings.showTags && this.settings.colorByTag ? makeTagColorFn(base) : base;
		this.renderer?.setColorFn(this.colorFn);
		this.panel?.refreshFolders(); // hues may have been re-issued, so the legend's dots have to follow
		this.panel?.syncShuffle();   // this is the only path through which colorGroups changes —— the shuffle button's premise is refreshed here too
	}

	/** A top-level folder's real colour on the graph, fed to the panel legend —— a legend that does not match the graph is decoration, not a legend */
	private folderHex(folder: string): string {
		if (folder === '') return '#9aa4b2'; // the root = palette's NEUTRAL
		// A probe node: colour groups match on node.id's `path:` prefix, so the id has to carry the folder prefix
		const probe: GraphNode = {
			id: `${folder}/`,
			name: folder,
			folderTop: folder,
			degree: 0,
			inDegree: 0,
			outDegree: 0,
			fileSize: 0,
			tags: [],
			unresolved: false,
			tag: false,
		};
		return `#${this.colorFn(probe).getHexString()}`;
	}

	private onDataChanged(): void {
		this.topTagList = topTags(this.store.data, 12, this.settings.tagLens);
		// Has anything **newly appeared** with this change.
		// Only turning things off with a filter gives 0 —— and then the remaining stars must stay put.
		let newcomers = 0;
		for (const n of this.store.data.nodes) if (!this.knownIds.has(n.id)) newcomers++;
		this.knownIds = new Set(this.store.data.nodes.map((n) => n.id));
		if (!this.renderer) return;
		this.restartLayoutAfterReveal = false;
		this.tour?.abort(); // the index is about to be reordered, so abort the tour rather than fly to the wrong node
		// Clear the selection layer belonging to the old index first; the Lens is rebuilt from the persisted tag id once the new data lands.
		this.selected = -1;
		this.renderer.setFocus(null);
		this.renderer.setSelectedLinks([], []);
		this.overlay?.setSelection(-1, new Set());
		this.applyColorFn(); // the folder set may have changed → re-issue hues + refresh the legend
		this.renderer.setData(this.store.data, this.store.positions);
		this.renderer.setGhostLinks(this.settings.showGhostEdges ? this.store.ghostLinks : []);
		this.overlay?.setData(this.store.data, this.graphRadius);
		this.applyTagLens();
		this.panel?.refreshTags();
		// Say something when a filter empties the graph —— an empty 3D view looks like a crash, and this is not one of the "obvious on trying" cases
		this.panel?.setFilterEmpty(this.store.isFiltered() && this.store.data.nodes.length === 0);
		// Re-heat gently **only when new nodes arrived** —— that is what lets a new star find its place.
		//
		// Heating after a mere turn-off scatters every remaining star again.  Turn off one topic and
		// the user expects a picture where "only those stars disappear"; until now the whole graph
		// spread itself out afresh.  (user report 2026-08-19)
		//
		// alpha 0 is a full stop —— forceWorker.ts:152 marks it settled immediately and runs no ticks
		// when initialAlpha < alphaMin.
		// On a cold start with the coordinates **clumped at a point**, separate the seeds now.
		//
		// A "have we seeded already" flag cannot catch this.  The web receives the graph
		// asynchronously, and if the saved settings then change the community basis, the store
		// **reallocates** the coordinate array and the seeds are thrown away wholesale.  The flag
		// says true while the coordinates sit near 0.  So it reads **the result** (the spread), not
		// the state —— which self-heals whatever the order.
		//
		// Measured (3,319 entities · 11 communities, 2026-08-19):
		//     collapsed        radius  119 · separation 0.059   ← the picture the user saw
		//     just after seeding  radius 1081
		//     after settling   radius 1272 · separation 6.026   ← 102×
		//  ⚠ The condition used to be `!this.warmStart && this.isCollapsed()`.  That one `!warmStart`
		//    is why **a reload was always clumped** —— a warm start restores the cache (radius 1528),
		//    then the data arrives and the store reallocates the coordinate array back to the seed
		//    radius (160), while the flag stays true and this healing is skipped.
		//    The comment directly above said "it reads the result, not the state" and it was reading
		//    the state.  The verdict is owned by `reseedDecision` (tests/spread.test.ts).
		const decision = reseedDecision({
			collapsed: this.isCollapsed(),
			warmStart: this.warmStart,
			newcomers,
		});
		if (decision.reseed) {
			this.seedByGroup();
			// The layout grows tenfold (119 → 1297).  Without re-placing the camera the screen goes
			// **entirely blank** —— the collapsed state settled first, so framedOnSettle is already
			// true and the re-framing at :458 below is skipped.
			this.framedOnSettle = false;
			this.framedRadius = 0;      // the radius shrinks sharply and grows again —— track it from scratch
			this.initLayout(decision.alpha);   // the seeds were just placed, so full settling is needed
			// Waiting for the settling event is wrong —— while 3,300 nodes run at alpha=1, settled
			// never arrives and the re-framing at :458 never fires.  The screen is entirely blank
			// throughout (a camera placed for radius 119 vs an actual 1297).
			// The seed coordinates are already fixed, so place it right now.
			this.recenter();
		} else {
			this.initLayout(decision.alpha);
		}
		this.wasSettled = false;
	}

	/** Worker first, falling back to the main-thread implementation when creation fails (a rare environment) */
	/** Drop the Worker layout and switch to the main thread.  Settling progress is not lost. */
	private fallbackToMainThread(): void {
		const alpha = this.layout.isSettled() ? 0.06 : 0.6;
		this.layout.dispose();
		this.layout = new MainThreadForceLayout();
		this.layout.init(this.store.data, this.store.positions, this.layoutParams(), alpha);
		new Notice(t('notice.workerFallback'));
	}

	/**
	 * On hovering the graph — highlight the whole community around the cursor and show its description.
	 * Untouched while something is selected (selection highlighting wins).
	 *
	 * Why it judges by **area** rather than picking one node
	 *   It used to use the folderTop of the node the hover picked (10px radius).  That only
	 *   responded with the cursor within 10px of a node's centre, so more than half the time
	 *   hovering a visible clump did nothing at all (measured hit rate 42%).  And 34% of what
	 *   was picked was 'other'.  The measurements are in AggregateRenderer.pickGroupAt's comment.
	 */
	private hoverGroupAt(px: number, py: number, w: number, h: number): void {
		if (!this.store.isKdbMode() || this.settings.kdbGroupBy !== 'community') return;
		if (this.selected >= 0) return;
		const nodes = this.store.data.nodes;
		const g = this.renderer?.pickGroupAt(px, py, w, h, GROUP_HOVER_PX, (i) => {
			const f = nodes[i]?.folderTop ?? null;
			// 'other' is the bin for things too small to keep.  Highlighting it means nothing, and
			// being sparsely scattered it keeps winning over a real community when left as a candidate.
			return f && f !== OTHER_GROUP ? f : null;
		}) ?? null;
		if (g !== this.hoveredGroup) {
			this.hoverGroup(g);
			this.panel?.showGroupTip(g);
		}
		// The card has to follow the cursor even when the community is unchanged, so it refreshes every time.
		const info = g ? this.store.kdb?.groupInfo.get(g) ?? null : null;
		this.overlay?.setGroupHover(
			g ? { name: g, summary: info?.summary ?? '', members: info?.members ?? '' } : null,
			px, py, w,
		);
	}

	/**
	 * Legend hover — light up that group alone and settle the rest.
	 *
	 * Kept from overlapping with selection (selectNode): hovering the legend and leaving it while
	 * a node is selected has to return to the original selection highlight.  So releasing a hover
	 * **re-applies the selection state** rather than clearing setFocus.
	 */
	/** Which name space the current filter belongs to.  It becomes the save/restore key. */
	private filterNamespace(): 'notes' | 'type' | 'community' {
		if (!this.store.isKdbMode()) return 'notes';
		return this.settings.kdbGroupBy === 'community' ? 'community' : 'type';
	}

	/** Take the active namespace's filter and apply it to the store and to settings. */
	private loadNamespacedFilter(): void {
		const ns = this.filterNamespace();
		const saved = this.settings.hiddenByNamespace[ns] ?? [];
		this.settings.hiddenFolders = [...saved];
		this.store.setHiddenFolders(saved);
	}

	/** Store the current filter into the active namespace.  Called whenever ControlPanel changes it. */
	saveNamespacedFilter(): void {
		this.settings.hiddenByNamespace[this.filterNamespace()] = [...this.settings.hiddenFolders];
		this.saveSoon();
	}

	/**
	 * Opens a note.  **An entity node is not a document.**
	 *
	 * Calling openLinkText with an id still shaped 'kdb:1234' falls into Obsidian's "create the
	 * missing note" path, leaving rubbish like 'kdb:1234.md' in the vault, which is then indexed.
	 * The browser build (web/main.ts) had this guard and the plugin side did not.
	 * (2026-08-18 adversarial review)
	 */
	private openDoc(id: string): void {
		if (!id || id.startsWith(KDB_PREFIX)) {
			new Notice('This node is an entity — click the source document path on the card');
			return;
		}
		void this.app.workspace.openLinkText(id, '', true);
	}

	private hoverGroup(group: string | null): void {
		if (!this.renderer) return;
		// The legend path and the graph path use the same state.  Kept separately, going from B in
		// the legend to A in the graph looks like 'already A' and the update is skipped.
		this.hoveredGroup = group;
		if (group === null) {
			if (this.selected >= 0) this.selectNode(this.selected, false);
			else this.renderer.setFocus(null);
			return;
		}
		const nodes = this.store.data.nodes;
		this.renderer.setFocus((i) => (nodes[i]?.folderTop === group ? SELECT_DIM.self : SELECT_DIM.rest));
	}

	/**
	 * The force pulling same-group nodes together.  **Each mode uses a different force.**
	 *
	 * The old comment said "both by-topic and by-type are on", and the `community` strength really
	 * was on for both.  And yet **the screen did not clump.**  Because what `community` pulls
	 * towards is the group's *current centre of mass* —— types are spread evenly across the whole
	 * graph, so all 8 centres of mass overlap in the middle and there is no direction to pull in.
	 * It tightens what is already separated; it does not separate.
	 *
	 * Topics (Louvain communities) separate on their own because links define the community and so
	 * take its side.  Types are the opposite —— concept and tool are densely connected, so links mix
	 * them instead.  Which is why type mode alone gets **a separate pull towards fixed anchors**.
	 *
	 * Measured (tests/typeClustering.test.ts · 400 nodes, 8 types · 1,200 random links):
	 *     anchor 0.00  separation 0.38   ← the screen the user saw
	 *     anchor 0.30  separation 3.47
	 *     anchor 0.60  separation 4.65   ← adopted
	 *     anchor 1.00  separation 5.94
	 *
	 * There are only 8 types, so the clumps are large.  If that feels heavy, raising the 'minimum
	 * connections' to reduce the node count is the better move (it is a density problem, not a count of clumps).
	 */
	private layoutParams(): import('../types').LayoutParams {
		const p = toLayoutParams(this.settings.physics);
		p.community = this.store.isKdbMode() ? COMMUNITY_PULL : 0;
		return p;
	}

	/** The anchor radius.  It has to use **the same formula** as `seedByGroup` so the seeds and the forces do not fight. */
	private anchorRadius(): number {
		return this.graphRadius * 4;
	}

	private initLayout(initialAlpha: number): void {
		const params = this.layoutParams();
		try {
			this.layout.init(this.store.data, this.store.positions, params, initialAlpha);
		} catch {
			if (this.layout instanceof MainThreadForceLayout) throw new Error('layout init failed');
			this.layout.dispose();
			this.layout = new MainThreadForceLayout();
			this.layout.init(this.store.data, this.store.positions, params, initialAlpha);
			new Notice(t('notice.workerFallback'));
		}
	}

	// ---------- quality tiers (M4) ----------

	/** Platform.isMobile is a hard ceiling; a manual override wins outright; auto = high + a watchdog */
	private pickTier(): QualityTier {
		if (Platform.isMobile) return TIERS.mobile;
		const o = this.settings.qualityOverride;
		if (o === 'high' || o === 'low' || o === 'mobile') return TIERS[o];
		return this.autoLow ? TIERS.low : TIERS.high;
	}

	applyTier(): void {
		const prev = this.tier.id;
		this.tier = this.pickTier();
		if (this.tier.id === 'mobile') this.tour?.abort(); // the mobile tier disables the tour (its panel section is hidden too)
		this.renderer?.applyTier(this.tier, this.settings.bloom.strength);
		this.overlay?.setBudgets(this.tier.hubLabels, this.tier.neighborLabels, this.tier.id === 'mobile');
		this.contentEl.toggleClass('gx-mobile', this.tier.id === 'mobile');
		const total = selectGraphFiles(this.app.vault.getFiles()).length;
		this.store.setCaps(this.tier.nodeCap, this.tier.linkCap); // triggers a rebuild when it changes
		if (this.tier.nodeCap !== null && total > this.tier.nodeCap && prev !== this.tier.id) {
			new Notice(t('notice.mobileCap', { n: this.tier.nodeCap, total }));
		}
	}

	/**
	 * The post-settling FPS watchdog (v0.3, bidirectional + hysteresis): the auto tier starts at high,
	 * giving the best quality first.
	 * At high: 3 consecutive 5s samples <30fps → drop to low.  At low: 4 consecutive 5s samples >55fps (with headroom) → back up to high.
	 * Different thresholds + different counts = hysteresis, preventing repeated flapping at the boundary.
	 */
	private watchdog(now: number): void {
		if (Platform.isMobile) return;
		if (this.settings.qualityOverride !== 'auto' || !this.layout.isSettled() || this.benchRunning || this.paused) return;
		if (now - this.lastWatchdogAt < 5000) return;
		this.lastWatchdogAt = now;
		const fps = this.hudFrames.length;
		if (fps <= 0) return;
		if (!this.autoLow) {
			if (fps < 30) {
				this.lowFpsChecks++;
				this.highFpsChecks = 0;
				if (this.lowFpsChecks >= 3) {
					this.autoLow = true;
					this.lowFpsChecks = 0;
					this.applyTier();
					new Notice(t('notice.watchdog'));
				}
			} else {
				this.lowFpsChecks = 0;
			}
		} else {
			if (fps > 55) {
				this.highFpsChecks++;
				this.lowFpsChecks = 0;
				if (this.highFpsChecks >= 4) {
					this.autoLow = false;
					this.highFpsChecks = 0;
					this.applyTier(); // there is headroom → back up to the highest quality
				}
			} else {
				this.highFpsChecks = 0;
			}
		}
	}

	// ---------- settings and visual direction ----------

	private applySettings(): void {
		const s = this.settings;
		this.renderer?.setBloomParams(s.bloom);
		this.renderer?.setNodeScale(s.look.nodeSize);
		this.renderer?.setLinkOpacity(s.look.linkOpacity);
		this.renderer?.setLinkCurve(s.look.linkCurve);
		this.renderer?.setSizeMode(s.look.sizeBy);
		this.renderer?.setStarfieldEnabled(s.showStarfield);
		this.syncNebulaTint();
		this.renderer?.setSpace(s.space);
		if (this.renderer) this.renderer.twinkleFreq = s.look.twinkle;
		if (this.director) {
			this.director.cruiseEnabled = s.cruise;
			this.director.cruiseSpeed = s.cruiseSpeed;
		}
	}

	/** The nebula backdrop's tint = the first two of the current colour groups (falling back to Hubble cyan/purple with no imported colours); called after a theme change, a shuffle or an import */
	private syncNebulaTint(): void {
		const g = this.settings.colorGroups;
		const a = g[0]?.color ?? '#46d4dc';
		const b = g[1]?.color ?? g[0]?.color ?? '#9a7fe0';
		this.renderer?.setNebulaTint(a, b);
	}

	/** A style preset = glow + physics + appearance + starfield + palette switched as a set (committed on click) */
	applyStylePreset(p: StylePreset): void {
		Object.assign(this.settings.bloom, p.bloom);
		Object.assign(this.settings.physics, p.physics);
		Object.assign(this.settings.look, p.look);
		Object.assign(this.settings.space, p.space);
		this.settings.showStarfield = p.starfield;
		this.settings.activePreset = p.id;
		this.applySettings(); // includes setStarfieldEnabled
		const theme = COLOR_THEMES.find((t) => t.id === p.theme);
		if (theme) this.applyColorTheme(theme); // apply and persist the palette
		this.layout.updateParams(this.layoutParams());
		this.wasSettled = false;
		if (p.frameElevDeg !== undefined) this.director?.setFramingElev(p.frameElevDeg);
		this.saveSoon();
	}

	/** Hover a preset: previews the "visual" parameters (glow/appearance/starfield/palette) at once, without persisting or re-heating the layout (physics is committed only on click) */
	previewStylePreset(p: StylePreset): void {
		const r = this.renderer;
		if (!r) return;
		r.setBloomParams(p.bloom);
		r.setNodeScale(p.look.nodeSize);
		r.setLinkOpacity(p.look.linkOpacity);
		r.setLinkCurve(p.look.linkCurve);
		r.twinkleFreq = p.look.twinkle;
		r.setSizeMode(p.look.sizeBy);
		r.setStarfieldEnabled(p.starfield);
		r.setSpace(p.space); // the nebula tint reuses the already-baked texture (a hover does not re-bake; only a committed click changes the colour)
		const theme = COLOR_THEMES.find((t) => t.id === p.theme);
		if (theme && this.settings.colorGroups.length > 0) {
			const temp = this.settings.colorGroups.map((g, i) => ({ ...g, color: theme.colors[i % theme.colors.length] ?? g.color }));
			this.applyColorFn(makeNodeColorFn(temp));
			r.recolor();
		}
	}

	/** End the preview: restore the "visual" parameters to the committed settings */
	endStylePreview(): void {
		const r = this.renderer;
		if (!r) return;
		const s = this.settings;
		r.setBloomParams(s.bloom);
		r.setNodeScale(s.look.nodeSize);
		r.setLinkOpacity(s.look.linkOpacity);
		r.setLinkCurve(s.look.linkCurve);
		r.twinkleFreq = s.look.twinkle;
		r.setSizeMode(s.look.sizeBy);
		r.setStarfieldEnabled(s.showStarfield);
		r.setSpace(s.space);
		this.applyColorFn();
		r.recolor();
	}

	// ---------- custom presets ----------

	/**
	 * Save the current parameters as a user preset.
	 * It asks for a name first —— with only an automatic name ("mine 3") the list becomes
	 * indistinguishable in no time, and undoing it means hunting for rename again.  The default is
	 * pre-filled, so one Enter finishes it.
	 */
	saveCurrentAsPreset(): void {
		const n = this.settings.customPresets.length + 1;
		new PromptModal(
			this.app,
			{ title: t('mine.nameTitle'), value: t('mine.name', { n }), cta: t('mine.saveCta') },
			(name) => {
				const id = `custom-${Date.now().toString(36)}`;
				const p: StylePreset = {
					id,
					name,
					nameEn: name,
					starfield: this.settings.showStarfield,
					space: { ...this.settings.space },
					theme: this.settings.colorTheme,
					bloom: { ...this.settings.bloom },
					physics: { ...this.settings.physics },
					look: { ...this.settings.look },
				};
				this.settings.customPresets.push(p);
				this.settings.activePreset = id;
				this.saveNow();
				this.panel?.refreshPresets();
				// Saving barely changes the screen —— without a toast there is no way to tell it was pressed
				new Notice(t('mine.saved', { name }));
			},
		).open();
	}

	moveCustomPreset(i: number, dir: -1 | 1): void {
		const a = this.settings.customPresets;
		const j = i + dir;
		if (j < 0 || j >= a.length) return;
		const tmp = a[i]!;
		a[i] = a[j]!;
		a[j] = tmp;
		this.saveNow();
		this.panel?.refreshPresets();
	}

	/**
	 * It cannot be undone, so it asks first.  The confirmation lives inside this method because
	 * this is the only path to deletion —— put it in the caller (the ⋯ menu) and some later
	 * caller deletes without asking.
	 */
	deleteCustomPreset(i: number): void {
		const target = this.settings.customPresets[i];
		if (!target) return;
		const name = target.nameEn ?? target.name;
		new PromptModal(
			this.app,
			{ title: t('mine.confirmDel'), body: t('mine.confirmDelBody', { name }), cta: t('mine.ok'), danger: true },
			() => {
				// The list may have changed while confirming.  Delete that preset, not the index.
				const at = this.settings.customPresets.indexOf(target);
				if (at < 0) return;
				const removed = this.settings.customPresets.splice(at, 1)[0];
				if (removed && this.settings.activePreset === removed.id) this.settings.activePreset = '';
				this.saveNow();
				this.panel?.refreshPresets();
				new Notice(t('mine.deleted', { name }));
			},
		).open();
	}

	/** Rename a custom preset: the name is written to both name and nameEn (both languages use it, with no fallback to "mine N"); written to disk at once */
	renameCustomPreset(i: number, name: string): void {
		const p = this.settings.customPresets[i];
		if (!p) return;
		const trimmed = name.trim();
		if (!trimmed) return;
		p.name = trimmed;
		p.nameEn = trimmed;
		this.saveNow();
		this.panel?.refreshPresets();
	}

	/** Section-level reset: return one section's parameters to the currently active preset's values */
	restorePresetSection(group: 'bloom' | 'physics' | 'look' | 'space'): void {
		const p = [...STYLE_PRESETS, ...this.settings.customPresets].find((x) => x.id === this.settings.activePreset);
		if (!p) return;
		if (group === 'bloom') Object.assign(this.settings.bloom, p.bloom);
		else if (group === 'physics') Object.assign(this.settings.physics, p.physics);
		else if (group === 'space') {
			Object.assign(this.settings.space, p.space);
			this.settings.showStarfield = p.starfield;
		} else {
			Object.assign(this.settings.look, p.look);
			const theme = COLOR_THEMES.find((t) => t.id === p.theme);
			if (theme) this.applyColorTheme(theme);
		}
		this.applySettings();
		if (group === 'physics') {
			this.layout.updateParams(this.layoutParams());
			this.wasSettled = false;
		}
		this.saveSoon();
		this.panel?.refreshAll();
	}

	/** Back to centre: clear the selection + glide back to the overview + cruise around the global centre on arrival */
	recenter(): void {
		this.clearSelection();
		const f = this.computeFraming();
		this.director?.resetView(f.center, f.radius, () => this.director?.beginFocusOrbit(null));
	}

	/**
	 * The node cloud's real framing parameters: the centre of mass + the 95th-percentile radius of the distance to it (so a stray orphan does not pull the camera too far or off-centre).
	 * Framing uses this rather than a fixed seed radius + the origin → however far the physics spreads it and however the centre drifts, everything stays framed and centred.  Falls back with no renderer.
	 */
	private computeFraming(): { center: Vector3; radius: number } {
		const r = this.renderer;
		const n = this.store.data.nodes.length;
		if (!r || n === 0) return { center: new Vector3(), radius: this.graphRadius };
		const tmp = new Vector3();
		const center = new Vector3();
		for (let i = 0; i < n; i++) center.add(r.nodePosition(i, tmp));
		center.divideScalar(n);
		const dists = new Float64Array(n);
		for (let i = 0; i < n; i++) dists[i] = r.nodePosition(i, tmp).distanceTo(center);
		dists.sort(); // a typed array sorts numerically ascending by default
		const idx = Math.min(n - 1, Math.floor(n * 0.95));
		return { center, radius: Math.max(dists[idx] ?? this.graphRadius, this.graphRadius * 0.3) };
	}

	/** Apply a colour theme: tint the existing colour groups in order (with no groups, generate them from the top-level folders by node count) */
	applyColorTheme(theme: ColorTheme): void {
		// In KDB mode the colours are decided by the data (export_kdb_graph's groupColors).
		//
		// Why it is blocked here —— the logic below builds the top 9 groups as `path:<name>` queries
		// and paints them with theme colours.  But an entity node's id is 'kdb:1234' and **never
		// matches that prefix**.  The result: the real nodes were drawn in the data's colours while
		// the legend, which builds its probe node ids as '<name>/', did match the prefix and took the
		// theme colour.  The legend and the picture end up different colours (measured, 3 of 6
		// disagreed: 'hybrid search architecture' graph #2cf266 vs legend #8134af).
		//
		// And a theme palette has 9 colours while there are over 20 topics, so it could never cover them anyway.
		if (this.store.isKdbMode()) {
			this.settings.colorGroups = [];
			this.settings.colorTheme = theme.id;
			this.applyColorFn();          // repaint with the data's (explicit) colours
			this.renderer?.recolor();
			this.syncNebulaTint();
			this.saveSoon();
			return;
		}
		let groups = this.settings.colorGroups;
		if (groups.length === 0) {
			const byFolder = new Map<string, number>();
			for (const n of this.store.data.nodes) {
				if (n.folderTop && !n.unresolved && !n.tag) byFolder.set(n.folderTop, (byFolder.get(n.folderTop) ?? 0) + 1);
			}
			groups = [...byFolder.entries()]
				.sort((a, b) => b[1] - a[1])
				.slice(0, 9)
				.map(([folder]) => ({ query: `path:${folder}`, color: '#9aa4b2' }));
			this.settings.colorGroups = groups;
		}
		groups.forEach((g, i) => (g.color = theme.colors[i % theme.colors.length] ?? g.color));
		this.settings.colorTheme = theme.id;
		this.applyColorFn(makeNodeColorFn(groups));
		this.renderer?.recolor();
		this.syncNebulaTint();
		this.saveSoon();
	}

	/** Trigger the creation animation by hand (with a note when the coordinates have not settled) */
	playRevealManually(): void {
		if (!this.layout.isSettled()) {
			new Notice(t('notice.notSettled'));
			return;
		}
		this.renderer?.playReveal();
		this.frameLoop?.resetClock();
	}

	/** Shuffle between the imported colour groups (the groups stay, the colours swap) */
	shuffleColors(): void {
		const groups = this.settings.colorGroups;
		if (groups.length < 2) {
			new Notice(t('notice.needImport'));
			return;
		}
		const colors = groups.map((g) => g.color);
		for (let i = colors.length - 1; i > 0; i--) {
			const j = Math.floor(Math.random() * (i + 1));
			[colors[i], colors[j]] = [colors[j]!, colors[i]!];
		}
		groups.forEach((g, i) => (g.color = colors[i] ?? g.color));
		this.settings.colorTheme = 'custom';
		this.applyColorFn(makeNodeColorFn(groups));
		this.renderer?.recolor();
		this.syncNebulaTint();
		this.saveSoon();
	}

	/** preset + app theme → tokens (adaptive dark shares the scene with deep space) */
	applyPreset(): void {
		if (!this.renderer) return;
		const isDark = this.contentEl.ownerDocument.body.hasClass('theme-dark'); // use the theme of the view's own window (popouts supported)
		const tokens = this.settings.preset === 'deep-space' || isDark ? DEEP_SPACE : DAYLIGHT;
		this.renderer.applyTokens(tokens, this.settings.bloom.strength);
		this.panel?.setPanelTheme(tokens.id === 'daylight' ? 'gx-theme-light' : 'gx-theme-dark');
		this.contentEl.toggleClass('gx-daylight', tokens.id === 'daylight');
	}

	/** workspace css-change (forwarded by the view) */
	onCssChange(): void {
		if (this.settings.preset === 'adaptive') this.applyPreset();
	}

	private async importColors(notify: boolean): Promise<void> {
		const groups = await readGraphColorGroups(this.app);
		if (!groups || groups.length === 0) {
			if (notify) new Notice(t('notice.noColorGroups'));
			return;
		}
		this.settings.colorGroups = groups;
		this.applyColorFn(makeNodeColorFn(groups));
		this.renderer?.recolor();
		this.syncNebulaTint();
		this.saveSoon();
		if (notify) new Notice(t('notice.imported', { n: groups.length }));
	}

	// ---------- selection / focus / search ----------

	openSearch(): void {
		new NodeSearchModal(this.app, this.store.data.nodes, (i) => this.selectNode(i, true)).open();
	}

	selectNode(index: number, fly: boolean): void {
		const renderer = this.renderer;
		const director = this.director;
		if (!renderer || !director) return;
		this.selected = index;
		// A CSR neighbourhood BFS to the configured depth (replacing the old O(all edges) scan)
		const { depthOf, linkTier1, linkTier2 } = neighborhood(this.store.adjacency, index, this.settings.selectionDepth);
		// The first-degree ring: labels and the orbit direction use the first degree only (the second dilutes the centre of mass)
		const ring1: number[] = [];
		for (const [i, dd] of depthOf) if (dd === 1) ring1.push(i);
		renderer.setFocus((i) => {
			const dd = depthOf.get(i);
			return dd === undefined ? SELECT_DIM.rest : dd === 0 ? SELECT_DIM.self : dd === 1 ? SELECT_DIM.d1 : SELECT_DIM.d2;
		});
		renderer.setSelectedLinks(linkTier1, linkTier2);
		this.overlay?.setSelection(index, new Set(ring1));
		if (fly) {
			const pos = renderer.nodePosition(index, new Vector3());
			// The first-degree centre-of-mass direction: on arrival, orbit sweeps the densely linked side first
			const density = new Vector3();
			let count = 0;
			const tmp = new Vector3();
			for (const ni of ring1) {
				density.add(renderer.nodePosition(ni, tmp));
				count++;
			}
			const densityDir = count > 0 ? density.divideScalar(count).sub(pos) : null;
			director.flyTo(pos, renderer.nodeRadius(index), () => director.beginFocusOrbit(densityDir));
		}
	}

	/** A settings toggle → switch the store, then reflect the new data in the overlay and renderer */
	async setKdbMode(on: boolean): Promise<void> {
		this.saveNamespacedFilter();          // notes ↔ entities are different name spaces too
		this.clearSelection();
		await this.store.setKdbMode(on);
		this.loadNamespacedFilter();
	}

	async reloadKdb(): Promise<void> {
		this.clearSelection();
		await this.store.reloadKdb();
	}

	async setKdbGroupBy(v: 'type' | 'community'): Promise<void> {
		// settings is written here as well.  Left to the panel callback alone, a programmatic call
		// leaves the store on communities while settings says types, and when the panel redraws,
		// the button highlight points at something other than reality.
		// The current namespace is stored **before** the switch.  Otherwise the filter in view a
		// moment ago disappears, and worse, leaks into the new namespace.
		this.saveNamespacedFilter();
		this.settings.kdbGroupBy = v;
		await this.store.setKdbGroupBy(v);
		this.loadNamespacedFilter();
		this.applyColorFn();          // the colour groups change wholesale
		// The grouping basis changed wholesale.  Re-separate the seeds on the new basis and heat ——
		// heating from the old layout alone leaves the previous basis's clumps intact.
		this.seedByGroup();
		this.initLayout(0.8);
	}

	async setKdbMinDegree(v: number): Promise<void> {
		this.clearSelection();
		await this.store.setKdbMinDegree(v);
	}

	/** Cycle the relation depth (1→2→3→4→1): flip the field, persist it, and recompute the highlight at once if something is selected (without moving the camera) */
	cycleSelectionDepth(): void {
		const next = { 1: 2, 2: 3, 3: 4, 4: 1 } as const;
		this.settings.selectionDepth = next[this.settings.selectionDepth];
		this.saveSoon();
		if (this.selected >= 0) this.selectNode(this.selected, false);
	}

	clearSelection(): void {
		this.selected = -1;
		this.overlay?.setSelection(-1, new Set());
		this.applyTagLens();
	}

	/** The Tag Lens does not build a second set of tag data: it reads the notes' tags directly; optional hubs only add visual nodes and edges. */
	private applyTagLens(): void {
		const resolved = resolveTagLens(this.store.data, this.store.adjacency, this.settings.showTags, this.settings.tagLens);
		if (this.settings.tagLens !== resolved.id) {
			// Tags disappearing after being turned off, or after a filter or rebuild: the persisted state has to be cleared with them.
			this.settings.tagLens = resolved.id;
			this.saveSoon();
		}
		this.panel?.refreshTags();
		if (this.selected >= 0) return; // an ordinary node selection temporarily overrides the Lens; this method is called again when the selection is cleared.
		if (!resolved.focus) {
			this.renderer?.setFocus(null);
			this.renderer?.setSelectedLinks([], []);
			return;
		}
		this.renderer?.setFocus((index) => (resolved.focus?.nodeIndices.has(index) ? SELECT_DIM.self : SELECT_DIM.rest));
		this.renderer?.setSelectedLinks(resolved.focus.linkIndices, []);
	}

	private toggleTagLens(tagId: string): void {
		if (!this.settings.showTags) return;
		this.settings.tagLens = nextTagLens(this.settings.tagLens, tagId);
		this.applyTagLens();
		this.saveSoon();
	}

	// ---------- tour / autopilot (v0.3; direction C = wander + connect two notes) ----------

	/** The panel's "wander" button / command: start or stop the ambient automatic tour */
	toggleTour(): void {
		if (!this.tour) return;
		try {
			if (this.tour.isRunning) {
				this.tour.stop();
				return;
			}
			this.tour.startWander(this.settings.tour.speed);
			// Give immediate feedback either way —— avoiding "I clicked and nothing happened": "started" if it can, otherwise the reason (no nodes yet, say)
			new Notice(this.tour.isRunning ? t('notice.tourStart') : t('notice.tourEmpty'));
		} catch (err) {
			this.tour?.abort();
			new Notice(t('notice.tourError', { msg: err instanceof Error ? err.message : String(err) }));
		}
	}

	/** Connect two notes: pick a start → pick an end → BFS shortest path → walk it node by node */
	startConnectTwo(): void {
		const nodes = this.store.data.nodes;
		if (nodes.length < 2) {
			new Notice(t('notice.tourEmpty'));
			return;
		}
		new Notice(t('notice.guidedPick')); // immediate feedback: a guided tour needs two points first, so the picker opens
		new NodeSearchModal(
			this.app,
			nodes,
			(startIdx) => {
				new NodeSearchModal(
					this.app,
					nodes,
					(endIdx) => {
						const path = shortestPath(this.store.adjacency, startIdx, endIdx);
						if (path.length < 2) {
							new Notice(t('notice.noPath'));
							return;
						}
						this.tour?.startGuided(path, this.settings.tour.speed);
						if (this.tour?.isRunning) new Notice(t('notice.tourStart'));
					},
					t('search.pickEnd'),
				).open();
			},
			t('search.pickStart'),
		).open();
	}

	setTourSpeed(): void {
		this.tour?.setSpeed(this.settings.tour.speed);
		this.saveSoon();
	}

	private flyToSelected(): void {
		if (this.selected < 0 || !this.renderer || !this.director) return;
		const pos = this.renderer.nodePosition(this.selected, new Vector3());
		this.director.flyTo(pos, this.renderer.nodeRadius(this.selected));
	}

	// ---------- picking ----------

	private bindPicking(dom: HTMLElement): void {
		let downX = 0;
		let downY = 0;
		const onDown = (e: PointerEvent) => {
			downX = e.clientX;
			downY = e.clientY;
		};
		const onUp = (e: PointerEvent) => {
			if (e.button !== 0 || e.ctrlKey || e.metaKey) return; // a pan gesture does not select
			if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
			const rect = dom.getBoundingClientRect();
			const i = this.renderer?.pickNearest(e.clientX - rect.left, e.clientY - rect.top, rect.width, rect.height, 14) ?? -1;
			if (i >= 0) this.selectNode(i, true);
			else this.clearSelection();
		};
		let hoverPending = false;
		const onMove = (e: PointerEvent) => {
			const throttle = this.tier.hoverThrottleMs;
			if (throttle === null || hoverPending) return; // the mobile tier: tap only, no hover
			hoverPending = true;
			window.setTimeout(() => {
				hoverPending = false;
				const renderer = this.renderer;
				if (!renderer) return;
				const rect = dom.getBoundingClientRect();
				const i = renderer.pickNearest(e.clientX - rect.left, e.clientY - rect.top, rect.width, rect.height, 10);
				this.overlay?.setHover(i);
				// In community mode it lights up **the whole community around the cursor**, not one node.
				// The same effect as sweeping the legend, obtained on the graph itself.
				this.hoverGroupAt(e.clientX - rect.left, e.clientY - rect.top, rect.width, rect.height);
				dom.style.cursor = i >= 0 ? 'pointer' : 'default';
			}, throttle);
		};
		// Leaving the canvas clears the highlight and the card.  Without it the last community is stuffed and mounted.
		const onLeave = () => {
			this.overlay?.setHover(-1);
			if (this.store.isKdbMode() && this.settings.kdbGroupBy === 'community' && this.selected < 0) {
				this.hoverGroup(null);
				this.panel?.showGroupTip(null);
			}
			this.overlay?.setGroupHover(null, 0, 0, 0);
		};
		const onKey = (e: KeyboardEvent) => {
			if (e.key === 'Escape') {
				this.clearSelection();
				e.preventDefault();
			}
		};
		// Double-click a selected node → open the note (the largest affordance for a public user)
		const onDblClick = (e: MouseEvent) => {
			if (e.button !== 0 || this.selected < 0) return;
			const node = this.store.data.nodes[this.selected];
			if (node && !node.unresolved && !node.tag) this.openDoc(node.id);
		};
		dom.addEventListener('pointerdown', onDown);
		dom.addEventListener('pointerup', onUp);
		dom.addEventListener('pointermove', onMove);
		dom.addEventListener('pointerleave', onLeave);
		dom.addEventListener('keydown', onKey);
		dom.addEventListener('dblclick', onDblClick);
		this.disposeFns.push(() => {
			dom.removeEventListener('pointerdown', onDown);
			dom.removeEventListener('pointerup', onUp);
			dom.removeEventListener('pointermove', onMove);
			dom.removeEventListener('pointerleave', onLeave);
			dom.removeEventListener('keydown', onKey);
			dom.removeEventListener('dblclick', onDblClick);
		});
	}

	// ---------- visibility pause ----------

	private bindVisibility(): void {
		this.visibilityBinding = new WindowVisibilityBinding(this.contentEl, (paused) => {
			if (this.paused !== paused) this.frameLoop?.resetClock();
			this.paused = paused;
		});
		this.rebindVisibility();
	}

	/**
	 * Binds the visibility check to "the window the view is currently in", supporting a move to a popout (issue #4):
	 * the old implementation used the global activeDocument + the main window's IntersectionObserver —— after the
	 * view moved to a new window, the observer treated an element already in the new window as invisible
	 * → paused=true → the render loop skipped → a black screen.
	 * This uses the visibility of the view's own document + the new window's IntersectionObserver (which is the right root).
	 */
	private rebindVisibility(): void {
		const doc = this.contentEl.ownerDocument;
		const win = doc.defaultView ?? window;
		if (this.boundWin !== win) {
			this.hudFrames = [];
			this.lastWatchdogAt = 0;
			this.lowFpsChecks = 0;
			this.highFpsChecks = 0;
		}
		this.boundWin = win;
		this.visibilityBinding?.bind(win, doc);
		// An rAF ID can only be cancelled by the Window that created it; hand over immediately on a window change rather than letting the old one run another frame.
		this.frameLoop?.setOwner(win);
	}

	// ---------- the control panel ----------

	private buildPanel(): void {
		this.panel = new ControlPanel(this.contentEl, this.settings, {
			onBloom: () => {
				this.renderer?.setBloomParams(this.settings.bloom);
				this.saveSoon();
			},
			onPhysics: () => {
				this.layout.updateParams(this.layoutParams());
				this.wasSettled = false;
				this.saveSoon();
			},
			onLook: () => {
				this.renderer?.setNodeScale(this.settings.look.nodeSize);
				this.renderer?.setLinkOpacity(this.settings.look.linkOpacity);
				this.renderer?.setLinkCurve(this.settings.look.linkCurve);
				if (this.renderer) this.renderer.twinkleFreq = this.settings.look.twinkle;
				this.saveSoon();
			},
			onSpace: () => {
				this.renderer?.setSpace(this.settings.space);
				this.saveSoon();
			},
			onSizeBy: () => {
				this.renderer?.setSizeMode(this.settings.look.sizeBy);
				this.saveSoon();
			},
			onCruise: (on) => {
				if (this.director) this.director.cruiseEnabled = on;
				this.saveSoon();
			},
			onPresetHover: (p) => this.previewStylePreset(p),
			onPresetHoverEnd: () => this.endStylePreview(),
			onSavePreset: () => this.saveCurrentAsPreset(),
			onMovePreset: (i, dir) => this.moveCustomPreset(i, dir),
			onDeletePreset: (i) => this.deleteCustomPreset(i),
			onRenamePreset: (i, name) => this.renameCustomPreset(i, name),
			onRestoreSection: (g) => this.restorePresetSection(g),
			onShowUnresolved: (on) => {
				this.store.setIncludeUnresolved(on);
				this.saveSoon();
			},
			onImportColors: () => void this.importColors(true),
			onShuffleColors: () => this.shuffleColors(),
			onColorTheme: (t) => this.applyColorTheme(t),
			onRecenter: () => this.recenter(),
			onKdbMode: (on) => {
				this.settings.kdbMode = on;
				this.saveSoon();
				void this.setKdbMode(on);
			},
			onGroupHover: (g) => this.hoverGroup(g),
			onFilterSaved: () => this.saveNamespacedFilter(),
			groupInfo: (g) => this.store.kdb?.groupInfo.get(g) ?? null,
			onKdbGroupBy: (v) => {
				this.settings.kdbGroupBy = v;
				this.saveNow();
				void this.setKdbGroupBy(v);
			},
			onKdbMinDegree: (v) => {
				this.settings.kdbMinDegree = v;
				this.saveSoon();
				void this.setKdbMinDegree(v);
			},
			onReveal: () => this.playRevealManually(),
			onShowOrphans: (on) => {
				this.store.setIncludeOrphans(on);
				this.saveSoon();
			},
			onShowTags: (on) => {
				if (!on) this.settings.tagLens = null;
				this.store.setIncludeTags(on);
				this.saveSoon();
			},
			onTagLens: (tagId) => this.toggleTagLens(tagId),
			getTopTags: () => this.topTagList,
			onTagColorMode: () => {
				this.applyColorFn();
				this.renderer?.recolor();
				this.saveSoon();
			},
			onTagHubs: () => {
				this.tagHubsSoon.cancel();
				this.store.setTagHubs(this.settings.showTagHubs, this.settings.tagHubLimit);
				this.saveSoon();
			},
			onTagHubLimit: () => {
				this.tagHubsSoon();
				this.saveSoon();
			},
			onResetTags: () => this.resetTags(),
			onHiddenFolders: (hidden) => {
				this.store.setHiddenFolders(hidden); // one click rebuilds; no debounce
				this.saveSoon();
			},
			onFilter: (q) => {
				this.filterSoon(q); // per-key would rebuild the graph and rerun the layout, so it must be debounced
				this.saveSoon();
			},
			onStarfield: (on) => {
				this.renderer?.setStarfieldEnabled(on);
				this.saveSoon();
			},
			onStylePreset: (p) => this.applyStylePreset(p),
			onCruiseSpeed: () => {
				if (this.director) this.director.cruiseSpeed = this.settings.cruiseSpeed;
				this.saveSoon();
			},
			onQuality: () => {
				this.autoLow = false;
				this.lowFpsChecks = 0;
				this.highFpsChecks = 0;
				this.applyTier();
				this.saveSoon();
			},
			onSearch: () => this.openSearch(),
			onTourToggle: () => this.toggleTour(),
			onConnectTwo: () => this.startConnectTwo(),
			onTourSpeed: () => this.setTourSpeed(),
			onSectionToggle: (id, open) => {
				this.settings.panelSections[id] = open;
				this.saveSoon();
			},
			getFolders: () => this.store.folders,
			folderColorHex: (f) => this.folderHex(f),
			onLanguage: (lang) => this.setLanguage(lang),
			onPanelWidth: (w) => {
				this.settings.panelWidth = w;
				this.saveSoon();
			},
			onReset: () => this.confirmResetLook(),
			runScenario: (s) => void this.runScenario(s),
		});
	}

	/**
	 * There is no way back, so it asks first.  The confirmation lives here for the same reason as
	 * in deleteCustomPreset —— the paths to a reset are kept in one place.
	 */
	private confirmResetLook(): void {
		new PromptModal(
			this.app,
			{ title: t('adv.resetLook'), body: t('confirm.resetLook'), cta: t('adv.resetLook'), danger: true },
			() => this.resetLook(),
		).open();
	}

	/**
	 * Resetting the look alone = the default "galaxy" preset (appearance, space, bloom, physics,
	 * starfield, palette) + automatic orbiting.
	 *
	 * It used to be called "reset everything", and it actually left quality, filters, tags,
	 * language and KDB mode alone (measured 2026-08-21: pressing it with quality on High leaves it
	 * on High).  Rather than widen the scope, the name was matched to it —— nobody wants a button
	 * that throws away their language and filters, and this repository already uses narrow resets
	 * like resetTags.
	 */
	private resetLook(): void {
		const galaxy = STYLE_PRESETS.find((p) => p.id === 'galaxy');
		this.settings.cruise = DEFAULT_SETTINGS.cruise;
		this.settings.cruiseSpeed = DEFAULT_SETTINGS.cruiseSpeed;
		if (galaxy) this.applyStylePreset(galaxy);
		if (this.director) {
			this.director.cruiseEnabled = this.settings.cruise;
			this.director.cruiseSpeed = this.settings.cruiseSpeed;
		}
		this.panel?.refreshAll();
	}

	/** Resets the tag visualisation alone, touching no preset, folder colour, filter or navigation. */
	private resetTags(): void {
		this.settings.tagLens = null;
		this.settings.colorByTag = DEFAULT_SETTINGS.colorByTag;
		this.settings.showTagHubs = DEFAULT_SETTINGS.showTagHubs;
		this.settings.tagHubLimit = DEFAULT_SETTINGS.tagHubLimit;
		this.tagHubsSoon.cancel();
		this.applyColorFn();
		this.renderer?.recolor();
		this.store.setTagHubs(this.settings.showTagHubs, this.settings.tagHubLimit);
		// The selection is cleared **only when it is a tag node**.
		//
		// It used to be an unconditional `clearSelection()`.  But turning tag hubs off removes only
		// the `tag:#…` nodes, and the **note** the user picked is still there.  Clearing that too
		// makes a button labelled "Reset tags" do something its label never said —— tidying up tags
		// and losing the note you were reading.
		const sel = this.selected >= 0 ? this.store.data.nodes[this.selected] : null;
		if (sel && sel.id.startsWith('tag:#')) this.clearSelection();
		else this.applyTagLens();     // keep the selection, but the lens release still has to be reflected
		this.panel?.refreshAll();
		this.saveSoon();
	}

	/** After a language switch: destroy the old panel and rebuild it in the current language */
	rebuildPanel(): void {
		this.panel?.dispose();
		this.panel = null;
		this.buildPanel();
	}

	/** The panel's top-bar language switch (auto + six languages) */
	setLanguage(pref: LangPref): void {
		this.settings.language = pref;
		setLang(resolveLang(pref));
		this.saveSoon();
		this.rebuildPanel();
		if (this.selected >= 0) this.selectNode(this.selected, false); // the card is rebuilt in the new language
	}

	/** After the settings page changes a durable preference, re-apply it to this view */
	syncFromSettings(): void {
		this.applySettings();
		this.applyPreset();
		this.autoLow = false;
		this.lowFpsChecks = 0;
		this.highFpsChecks = 0;
		this.applyTier(); // internally re-picks the tier from qualityOverride + store.setCaps
		this.store.setIncludeOrphans(this.settings.showOrphans);
		this.store.setIncludeUnresolved(this.settings.showUnresolved);
		this.store.setTagHubs(this.settings.showTagHubs, this.settings.tagHubLimit);
		this.store.setIncludeTags(this.settings.showTags);
		this.store.setShowGhostEdges(this.settings.showGhostEdges);
		this.applyColorFn();
		this.renderer?.recolor();
		this.applyTagLens();
		this.panel?.refreshAll();
	}

	private updateHud(now: number): void {
		this.hudFrames.push(now); // recorded every frame (for the watchdog); only the text is throttled
		while (this.hudFrames.length > 0 && now - (this.hudFrames[0] ?? 0) > 1000) this.hudFrames.shift();
		if (now % 500 > 250) return;
		const c = this.counts;
		this.panel?.statsEl?.setText(t('hud.notes', { n: c.nodes })); // the header: the note count alone
		this.panel?.advStatsEl?.setText(
			`${this.hudFrames.length} fps · ${this.renderer?.drawCalls ?? 0} calls · ${c.nodes}n/${c.links}l · ` +
				`${this.layout.isSettled() ? t('hud.settled') : t('hud.layouting')}`,
		); // fps and the technical figures move down into "advanced"
	}

	// ---------- the benchmark (same scene semantics as M0/M1) ----------

	private waitSettle(timeoutMs = 120_000): Promise<void> {
		return new Promise((resolve) => {
			const t0 = performance.now();
			const check = () => {
				if (this.layout.isSettled() || performance.now() - t0 > timeoutMs) resolve();
				else window.setTimeout(check, 100);
			};
			check();
		});
	}

	async runScenario(scenario: 'S1' | 'S2' | 'S3'): Promise<BenchResult | null> {
		if (this.benchRunning || !this.renderer || !this.director) return null;
		this.benchRunning = true;
		try {
			if (scenario === 'S2') return await this.benchColdLayout();
			return await this.benchOrbit(scenario);
		} finally {
			this.benchRunning = false;
		}
	}

	private async benchOrbit(scenario: 'S1' | 'S3'): Promise<BenchResult> {
		const renderer = this.renderer;
		const director = this.director;
		if (!renderer || !director) throw new Error('not ready');

		const wantUnresolved = scenario === 'S3';
		if (this.store.getIncludeUnresolved() !== wantUnresolved) {
			this.store.setIncludeUnresolved(wantUnresolved);
		}
		new Notice(`${scenario}: waiting for layout to settle…`);
		await this.waitSettle();
		if (renderer.getBloomStrength() < 0.01) renderer.setBloomStrength(0.9);
		await sleep(300);

		this.benchMode = true;
		const target = director.target.clone();
		const sph = new Spherical().setFromVector3(renderer.camera.position.clone().sub(target));
		new Notice(`${scenario}: 20s orbit, measuring fps…`);
		const stats = await collectFrames(20_000, (elapsed) => {
			const angle = sph.theta + (elapsed / 20_000) * Math.PI * 2;
			renderer.camera.position.setFromSpherical(new Spherical(sph.radius, sph.phi, angle)).add(target);
			renderer.camera.lookAt(target);
		});
		this.benchMode = false;

		const result: BenchResult = {
			scenario,
			timestamp: new Date().toISOString(),
			nodes: this.counts.nodes,
			links: this.counts.links,
			bloom: renderer.getBloomStrength() > 0,
			drawCalls: renderer.drawCalls,
			renderer: 'aggregate',
			...stats,
		};
		await writeBenchResult(this.app, result);
		new Notice(`${scenario} done: avg ${stats.avgFps.toFixed(1)} fps · ${renderer.drawCalls} calls`);
		return result;
	}

	private async benchColdLayout(): Promise<BenchResult> {
		new Notice('S2: cold layout start (budgeted ticks — UI should stay responsive)…');
		if (this.store.getIncludeUnresolved()) this.store.setIncludeUnresolved(false);
		await sleep(300);
		const longTasks = observeLongTasks();
		const t0 = performance.now();
		this.settings.positionCache = {}; // a cold layout must have no warm start
		this.settings.positionCacheKey = '';
		this.store.rebuild(false); // triggers onDataChanged (alpha 0.3) …
		this.initLayout(1); // … and immediately re-ignites with full cold-layout semantics (correcting the semantic drift introduced at M2)
		this.wasSettled = false;
		await this.waitSettle();
		const settleMs = performance.now() - t0;
		const ticks = this.layout.ticks;
		const lt = longTasks.stop();

		const result: BenchResult = {
			scenario: 'S2',
			timestamp: new Date().toISOString(),
			nodes: this.counts.nodes,
			links: this.counts.links,
			bloom: (this.renderer?.getBloomStrength() ?? 0) > 0,
			renderer: 'aggregate',
			settleMs,
			ticks,
			avgTickMs: ticks > 0 ? settleMs / ticks : -1,
			longTaskCount: lt.count,
			longestTaskMs: lt.longestMs,
			longTaskTotalMs: lt.totalMs,
		};
		await writeBenchResult(this.app, result);
		new Notice(`S2 done: settled ${(settleMs / 1000).toFixed(1)}s / ${ticks} ticks, longest block ${lt.longestMs.toFixed(0)}ms`);
		return result;
	}

	// ---------- the destruction contract ----------

	dispose(): void {
		if (this.disposed) return;
		this.disposed = true;
		this.tagHubsSoon.cancel();
		this.store.unload();
		this.frameLoop?.dispose();
		this.frameLoop = null;
		this.visibilityBinding?.dispose();
		this.visibilityBinding = null;
		this.boundWin = null;
		for (const fn of this.disposeFns) fn();
		this.disposeFns = [];
		this.maskEl?.remove();
		this.maskEl = null;
		this.restartLayoutAfterReveal = false;
		this.overlay?.dispose();
		this.overlay = null;
		this.director?.dispose();
		this.director = null;
		this.layout.dispose();
		this.renderer?.dispose();
		this.renderer = null;
		this.panel?.dispose();
		this.panel = null;
	}
}
