import {
	ACESFilmicToneMapping,
	BufferAttribute,
	BufferGeometry,
	Color,
	DynamicDrawUsage,
	Group,
	LineBasicMaterial,
	LineDashedMaterial,
	LineSegments,
	NoToneMapping,
	PerspectiveCamera,
	Points,
	PointsMaterial,
	Scene,
	ShaderMaterial,
	Vector2,
	Vector3,
	WebGLRenderer,
} from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import type { GraphData } from '../types';
import type { SpaceSettings } from '../settings';
import { BLOOM_DEFAULTS, NODE_BASE_RADIUS, NODE_MAX_RADIUS, STARFIELD_ROTATION_RAD_PER_S } from '../constants';
import {
	NODE_FRAGMENT_SHADER,
	NODE_VERTEX_SHADER,
	patchRevealLineShader,
} from './shaders';
import { linkColor, fallbackColorFn } from './palette';
import type { NodeColorFn } from './palette';
import { buildFieldStars, buildStarfield, disposeStarfield, Twinkler } from './starfield';
import { fillLinkPositions, fillRevealLinkAttributes, segsFor } from './linkCurves';
import { fitGraphPositions, GRAPH_FIT_RADIUS_FACTOR } from './graphTransform';
import { ClusterClouds, NebulaDome } from './nebula';
import type { VisualTokens } from './presets';
import { effectivePixelRatio, type QualityTier } from '../quality/tiers';
import { DEEP_SPACE } from './presets';
import { maxPositionRadius, revealScale } from './reveal';

const FOCUS_FADE_S = 0.28;

/**
 * The aggregate renderer: every node in 1× Points, every link in 1× LineSegments, the starfield
 * in 3× Points, the selection's highlighted links in 1× LineSegments.  The whole scene is <10 draw calls.
 * The visual direction (deep space / daylight) switches through VisualTokens, with no WebGL rebuild.
 */
export class AggregateRenderer {
	readonly camera: PerspectiveCamera;
	readonly renderer: WebGLRenderer;

	private scene = new Scene();
	private composer: EffectComposer;
	private bloomPass: UnrealBloomPass;
	private outputPass: OutputPass;
	private renderPass: RenderPass;

	private nodePoints: Points | null = null;
	private nodeMaterial: ShaderMaterial | null = null;
	private nodeGeometry: BufferGeometry | null = null;
	private linkSegments: LineSegments | null = null;
	private linkGeometry: BufferGeometry | null = null;
	private linkMaterial: LineBasicMaterial | null = null;
	private revealLinkMaterial: LineBasicMaterial | null = null;
	private selSegments: LineSegments | null = null;
	private selGeometry: BufferGeometry | null = null;
	private selMaterial: LineBasicMaterial | null = null;
	private revealSelMaterial: LineBasicMaterial | null = null;
	// —— ghost edges (the Constellation pending-suggestion layer, a dashed dim layer; taking no part in layout, adjacency or picking) ——
	private ghostSegments: LineSegments | null = null;
	private ghostGeometry: BufferGeometry | null = null;
	private ghostMaterial: LineDashedMaterial | null = null;
	private ghostLinks: { source: number; target: number; score: number }[] = [];
	private selLinkIdx: number[] = []; // tier1+tier2 merged, used by updateSelPositions
	private selLinks: ({ source: number; target: number } | undefined)[] = []; // the highlighted-edge subset (for filling the curves)
	private selTier1: number[] = [];
	private selTier2: number[] = [];
	private starfield: Group;
	private twinkler: Twinkler;
	private starfieldEnabled = true;
	twinkleFreq = 0.5;
	private motes: Points | null = null;
	// —— curved links (v0.4) ——
	private linkCurvature = 0;
	private tierLinkSegs = 8;
	private linkK = 1; // the current link geometry's segments per edge (always 1 at curvature 0 = the old straight path)
	// —— deep-space background shape layers (v0.4) ——
	private space: SpaceSettings = { nebula: 0, fieldStars: 0, clusterClouds: 0 };
	private nebula: NebulaDome | null = null;
	private clouds: ClusterClouds | null = null;
	private fieldStars: Points<BufferGeometry, PointsMaterial> | null = null;
	private fieldStarsDensity = 0; // the density already built (including the -1 that forces a rebuild on a tier change)
	private nebulaTintA = '#46d4dc';
	private nebulaTintB = '#9a7fe0';
	private tierCloudsAllowed = true;
	private tierStarScale = 1;
	private reveal: { elapsedMs: number; durMs: number; maxR: number; target: Float32Array } | null = null;
	private revealActiveUniform = { value: 0 };
	private revealProgressUniform = { value: 1 };
	private revealMaxRadiusUniform = { value: 1 };
	private linkCurvatureUniform = { value: 0 };

	private data: GraphData = { nodes: [], links: [] };
	/** The physics simulation's raw coordinates (the single truth of the Worker's write-back and the warm-start cache; read, never written) */
	private positions: Float32Array = new Float32Array(0);
	/** The CPU-fitted end coordinates; unchanged during the creation animation, with overlays and picking computing the current display coordinates on demand */
	private renderPositions: Float32Array = new Float32Array(0);
	/** The GPU nodes' target coordinates: normally following renderPositions; held as an immutable endpoint during creation */
	private nodeTargetPositions: Float32Array = new Float32Array(0);
	/** The weighting degree used by fitGraphPositions, rebuilt with setData */
	private fitWeights: Float32Array = new Float32Array(0);
	private sizes: Float32Array = new Float32Array(0);
	private dimCurrent: Float32Array = new Float32Array(0);
	private dimTarget: Float32Array = new Float32Array(0);
	private dimAnimating = false;

	private colorFn: NodeColorFn = fallbackColorFn;
	private tokens: VisualTokens = DEEP_SPACE;
	private tierBloomAllowed = true;
	private lastW = 2;
	private lastH = 2;
	private baseLinkOpacity = 0.16;
	private focusActive = false;
	private graphRadiusEstimate: number;

	private projVec = new Vector3();
	private ghostSourceVec = new Vector3();
	private ghostTargetVec = new Vector3();
	private pixelScale = 1;
	private nodeScale = 1;

	constructor(container: HTMLElement, graphRadiusEstimate: number) {
		this.graphRadiusEstimate = graphRadiusEstimate;
		this.renderer = new WebGLRenderer({ antialias: false, alpha: false });
		this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
		this.renderer.toneMapping = ACESFilmicToneMapping;
		this.renderer.toneMappingExposure = 1.05;
		this.renderer.info.autoReset = false;
		container.appendChild(this.renderer.domElement);

		this.scene.background = new Color(this.tokens.background);
		this.camera = new PerspectiveCamera(60, 1, 0.5, 50_000);

		const sf = buildStarfield(graphRadiusEstimate * 6.5);
		this.starfield = sf.group;
		this.twinkler = sf.twinkler;
		this.scene.add(this.starfield);

		this.composer = new EffectComposer(this.renderer);
		this.renderPass = new RenderPass(this.scene, this.camera);
		this.bloomPass = new UnrealBloomPass(
			new Vector2(2, 2),
			BLOOM_DEFAULTS.strength,
			BLOOM_DEFAULTS.radius,
			BLOOM_DEFAULTS.threshold,
		);
		this.outputPass = new OutputPass();
		this.composer.addPass(this.renderPass);
		this.composer.addPass(this.bloomPass);
		this.composer.addPass(this.outputPass);
	}

	// ---------- data and colour ----------

	setColorFn(fn: NodeColorFn): void {
		this.colorFn = fn;
	}

	setData(data: GraphData, positions: Float32Array): void {
		this.resetRevealState();
		this.data = data;
		this.positions = positions;
		this.disposeGraphObjects();

		const n = data.nodes.length;

		// —— nodes ——
		this.renderPositions = new Float32Array(n * 3);
		this.nodeTargetPositions = new Float32Array(n * 3);
		this.fitWeights = new Float32Array(n);
		const ghost = new Float32Array(n);
		this.sizes = new Float32Array(n);
		this.dimCurrent = new Float32Array(n).fill(1);
		this.dimTarget = new Float32Array(n).fill(1);
		for (let i = 0; i < n; i++) {
			const node = data.nodes[i];
			if (!node) continue;
			ghost[i] = node.unresolved ? 1 : 0;
			this.fitWeights[i] = node.degree;
			this.sizes[i] = this.computeSize(node);
		}
		this.fitPositions();
		this.nodeTargetPositions.set(this.renderPositions);
		this.nodeGeometry = new BufferGeometry();
		this.nodeGeometry.setAttribute(
			'position',
			new BufferAttribute(this.nodeTargetPositions, 3).setUsage(DynamicDrawUsage),
		);
		this.nodeGeometry.setAttribute('color', new BufferAttribute(new Float32Array(n * 3), 3));
		this.nodeGeometry.setAttribute('aSize', new BufferAttribute(this.sizes, 1));
		this.nodeGeometry.setAttribute('aGhost', new BufferAttribute(ghost, 1));
		this.nodeGeometry.setAttribute('aDim', new BufferAttribute(this.dimCurrent, 1));
		this.nodeMaterial = new ShaderMaterial({
			vertexShader: NODE_VERTEX_SHADER,
			fragmentShader: NODE_FRAGMENT_SHADER,
			vertexColors: true,
			transparent: true,
			depthWrite: false,
			uniforms: {
				uPixelScale: { value: this.pixelScale },
				uSizeMul: { value: this.nodeScale },
				uLightMode: { value: this.tokens.lightMode ? 1 : 0 },
				uMaxPoint: { value: 110 * this.renderer.getPixelRatio() },
				uRevealActive: this.revealActiveUniform,
				uRevealProgress: this.revealProgressUniform,
				uRevealMaxRadius: this.revealMaxRadiusUniform,
			},
		});
		this.nodePoints = new Points(this.nodeGeometry, this.nodeMaterial);
		this.nodePoints.renderOrder = 1; // nodes always cover the link mesh
		this.nodePoints.frustumCulled = false;
		this.scene.add(this.nodePoints);

		// —— links —— (curves: a K-segment polyline per edge; K=1 = exactly equivalent to the old straight rendering)
		this.buildLinkLayer();
		this.clouds?.clear(); // the cluster member indices go stale after a data rebuild; recomputed at the next settling

		this.recolor();
		this.updatePositions();
		this.buildSelLayer(); // restore the highlight layer after a data rebuild
	}

	/** The link layer's geometry is allocated for the current K (curvature/tier); rebuilt when curvature crosses 0↔>0 or the tier changes (a one-off, milliseconds) */
	private buildLinkLayer(): void {
		if (this.linkSegments) {
			this.scene.remove(this.linkSegments);
			this.linkGeometry?.dispose();
			this.linkMaterial?.dispose();
		}
		const m = this.data.links.length;
		this.linkK = segsFor(this.linkCurvature, this.tierLinkSegs);
		this.linkGeometry = new BufferGeometry();
		this.linkGeometry.setAttribute('position', new BufferAttribute(new Float32Array(m * this.linkK * 2 * 3), 3));
		this.linkGeometry.setAttribute('color', new BufferAttribute(new Float32Array(m * this.linkK * 2 * 3), 3));
		this.linkMaterial = new LineBasicMaterial({
			vertexColors: true,
			transparent: true,
			opacity: this.effectiveLinkOpacity(),
			depthWrite: false,
		});
		this.linkSegments = new LineSegments(this.linkGeometry, this.linkMaterial);
		this.linkSegments.renderOrder = 0;
		this.linkSegments.frustumCulled = false;
		this.scene.add(this.linkSegments);
	}

	/** Ghost-edge data (node indices + a strength of 0..1); an empty array = remove that layer */
	setGhostLinks(links: { source: number; target: number; score: number }[]): void {
		this.ghostLinks = links;
		this.buildGhostLayer();
	}

	private buildGhostLayer(): void {
		if (this.ghostSegments) {
			this.scene.remove(this.ghostSegments);
			this.ghostGeometry?.dispose();
			this.ghostMaterial?.dispose();
			this.ghostSegments = null;
			this.ghostGeometry = null;
			this.ghostMaterial = null;
		}
		const m = this.ghostLinks.length;
		if (m === 0) return;
		this.ghostGeometry = new BufferGeometry();
		this.ghostGeometry.setAttribute('position', new BufferAttribute(new Float32Array(m * 2 * 3), 3));
		const colors = new Float32Array(m * 2 * 3);
		const base = this.tokens.lightMode ? 0.35 : 0.8; // dark grey for a light theme, light grey for deep space
		for (let i = 0; i < m; i++) {
			const s = this.ghostLinks[i]?.score ?? 0.5;
			const v = base * (0.45 + 0.55 * s); // the score maps to brightness (deferred is already ×0.6 on the data side)
			for (let k = 0; k < 2; k++) {
				colors[(i * 2 + k) * 3] = v;
				colors[(i * 2 + k) * 3 + 1] = v;
				colors[(i * 2 + k) * 3 + 2] = v * 1.08; // a slight blue shift, to feel different from a solid line
			}
		}
		this.ghostGeometry.setAttribute('color', new BufferAttribute(colors, 3));
		this.ghostMaterial = new LineDashedMaterial({
			vertexColors: true,
			transparent: true,
			opacity: 0.22,
			depthWrite: false,
			dashSize: 2.6,
			gapSize: 3.4,
		});
		this.ghostSegments = new LineSegments(this.ghostGeometry, this.ghostMaterial);
		this.ghostSegments.renderOrder = 0;
		this.ghostSegments.frustumCulled = false;
		this.scene.add(this.ghostSegments);
		this.updateGhostPositions();
	}

	private updateGhostPositions(): void {
		if (!this.ghostGeometry || !this.ghostSegments) return;
		const attr = this.ghostGeometry.getAttribute('position') as BufferAttribute;
		const arr = attr.array as Float32Array;
		for (let i = 0; i < this.ghostLinks.length; i++) {
			const l = this.ghostLinks[i];
			if (!l) continue;
			this.displayPosition(l.source, this.ghostSourceVec);
			this.displayPosition(l.target, this.ghostTargetVec);
			arr[i * 6] = this.ghostSourceVec.x;
			arr[i * 6 + 1] = this.ghostSourceVec.y;
			arr[i * 6 + 2] = this.ghostSourceVec.z;
			arr[i * 6 + 3] = this.ghostTargetVec.x;
			arr[i * 6 + 4] = this.ghostTargetVec.y;
			arr[i * 6 + 5] = this.ghostTargetVec.z;
		}
		attr.needsUpdate = true;
		this.ghostSegments.computeLineDistances(); // dashed-segment distances follow the coordinates (≤500 edges, nanoseconds)
	}

	/** Link curvature 0–1 (the slider); crossing 0↔>0 changes the segment count and needs a geometry rebuild + recolour, otherwise only the vertices are refilled */
	setLinkCurve(v: number): void {
		this.linkCurvature = v;
		this.linkCurvatureUniform.value = v;
		if (segsFor(v, this.tierLinkSegs) !== this.linkK) {
			this.buildLinkLayer();
			this.recolor();
			this.buildSelLayer();
			if (this.reveal) this.activateRevealLinkLayer(this.reveal.target);
		}
		if (!this.reveal) this.updatePositions();
	}

	private sizeMode: 'degree' | 'fileSize' | 'uniform' = 'degree';

	private computeSize(node: import('../types').GraphNode): number {
		switch (this.sizeMode) {
			case 'fileSize':
				// The median note is ~2KB; a cube root compresses the long tail so a giant document does not swallow the screen
				return Math.min(Math.max(NODE_BASE_RADIUS * (0.7 + 1.1 * Math.cbrt(node.fileSize / 4096)), 1.6), NODE_MAX_RADIUS);
			case 'uniform':
				return NODE_BASE_RADIUS * 1.3;
			default:
				return Math.min(NODE_BASE_RADIUS * (1 + 0.5 * Math.sqrt(node.degree)), NODE_MAX_RADIUS);
		}
	}

	setSizeMode(mode: 'degree' | 'fileSize' | 'uniform'): void {
		this.sizeMode = mode;
		if (!this.nodeGeometry) return;
		for (let i = 0; i < this.data.nodes.length; i++) {
			const node = this.data.nodes[i];
			if (node) this.sizes[i] = this.computeSize(node);
		}
		(this.nodeGeometry.getAttribute('aSize') as BufferAttribute).needsUpdate = true;
	}

	/**
	 * The creation animation (G2.5 feedback): nodes bloom outwards from the centre in radius waves to their settled coordinates.
	 * The target coordinates are snapshotted once; nodes and links expand on the same progress on the GPU, while the CPU only maintains the overlay query coordinates.
	 */
	playReveal(durMs = 2600): void {
		const n = this.data.nodes.length;
		if (n === 0 || !this.nodeGeometry || !this.linkGeometry) return;

		// Both a replay and a data change restore the latest full coordinates first, so the previous round's display state is not inherited.
		this.resetRevealState();
		this.updatePositions();
		const target = new Float32Array(this.renderPositions);
		const maxR = maxPositionRadius(target, n);
		this.reveal = { elapsedMs: 0, durMs: Math.max(durMs, 1), maxR, target };
		this.revealActiveUniform.value = 1;
		this.revealProgressUniform.value = 0;
		this.revealMaxRadiusUniform.value = maxR;
		this.activateRevealLinkLayer(target);
		this.activateRevealSelectionLayer(target);
		if (this.ghostMaterial) this.ghostMaterial.opacity = 0;
		this.updateGhostPositions();
		// Compilation happens during the mask's fade-out; this keeps the first shader program's build cost out of the animation's first visible frame.
		this.renderer.compile(this.scene, this.camera);
	}

	private stepReveal(animationDeltaS: number): void {
		const reveal = this.reveal;
		if (!reveal) return;
		const safeDeltaS = Number.isFinite(animationDeltaS) ? Math.max(animationDeltaS, 0) : 0;
		reveal.elapsedMs += safeDeltaS * 1000;
		const p = Math.min(reveal.elapsedMs / reveal.durMs, 1);
		this.revealProgressUniform.value = p;
		this.syncRevealLinkOpacity(p);
		if (this.ghostMaterial) this.ghostMaterial.opacity = 0.22 * Math.min(p * 1.6, 1);
		this.updateGhostPositions(); // ≤500 suggestion edges; keeps the dashes at world scale so the shader does not compress the dash pattern
		if (p >= 1) {
			this.resetRevealState();
			this.updatePositions();
			this.refreshClusterClouds();
		}
	}

	private fillRevealAttributes(
		geometry: BufferGeometry,
		target: Float32Array,
		links: readonly ({ source: number; target: number } | undefined)[],
		K: number,
	): void {
		const vertexCount = links.length * K * 2;
		const floatCount = vertexCount * 3;
		const existingSource = geometry.getAttribute('aSourcePosition') as BufferAttribute | undefined;
		const existingTarget = geometry.getAttribute('aTargetPosition') as BufferAttribute | undefined;
		const existingT = geometry.getAttribute('aCurveT') as BufferAttribute | undefined;
		const reusable =
			existingSource?.array instanceof Float32Array &&
			existingSource.array.length === floatCount &&
			existingTarget?.array instanceof Float32Array &&
			existingTarget.array.length === floatCount &&
			existingT?.array instanceof Float32Array &&
			existingT.array.length === vertexCount;
		const source = reusable ? (existingSource.array as Float32Array) : new Float32Array(floatCount);
		const destination = reusable ? (existingTarget.array as Float32Array) : new Float32Array(floatCount);
		const curveT = reusable ? (existingT.array as Float32Array) : new Float32Array(vertexCount);
		fillRevealLinkAttributes(source, destination, curveT, target, links, K);
		if (!reusable) {
			geometry.setAttribute('aSourcePosition', new BufferAttribute(source, 3));
			geometry.setAttribute('aTargetPosition', new BufferAttribute(destination, 3));
			geometry.setAttribute('aCurveT', new BufferAttribute(curveT, 1));
		} else {
			existingSource.needsUpdate = true;
			existingTarget.needsUpdate = true;
			existingT.needsUpdate = true;
		}
	}

	private createRevealLinkMaterial(opacity: number): LineBasicMaterial {
		const material = new LineBasicMaterial({
			vertexColors: true,
			transparent: true,
			opacity,
			depthWrite: false,
		});
		material.onBeforeCompile = (shader) => {
			patchRevealLineShader(shader, {
				uLinkCurvature: this.linkCurvatureUniform,
				uRevealActive: this.revealActiveUniform,
				uRevealProgress: this.revealProgressUniform,
				uRevealMaxRadius: this.revealMaxRadiusUniform,
			});
		};
		material.customProgramCacheKey = () => 'galaxy-reveal-basic-v1';
		return material;
	}

	private activateRevealLinkLayer(target: Float32Array): void {
		const geometry = this.linkGeometry;
		const segments = this.linkSegments;
		if (!geometry || !segments) return;
		this.fillRevealAttributes(geometry, target, this.data.links, this.linkK);
		this.linkCurvatureUniform.value = this.linkCurvature;
		this.revealLinkMaterial ??= this.createRevealLinkMaterial(this.effectiveLinkOpacity());
		this.revealLinkMaterial.opacity = this.effectiveLinkOpacity() * Math.min(this.revealProgressUniform.value * 1.6, 1);
		segments.material = this.revealLinkMaterial;
	}

	private activateRevealSelectionLayer(target: Float32Array): void {
		if (!this.selGeometry || !this.selSegments || this.selLinks.length === 0) return;
		this.fillRevealAttributes(this.selGeometry, target, this.selLinks, this.linkK);
		this.revealSelMaterial ??= this.createRevealLinkMaterial(0.85);
		this.revealSelMaterial.opacity = 0.85 * Math.min(this.revealProgressUniform.value * 1.6, 1);
		this.selSegments.material = this.revealSelMaterial;
	}

	private syncRevealLinkOpacity(progress: number): void {
		const fade = Math.min(Math.max(progress, 0) * 1.6, 1);
		if (this.revealLinkMaterial) this.revealLinkMaterial.opacity = this.effectiveLinkOpacity() * fade;
		if (this.revealSelMaterial) this.revealSelMaterial.opacity = 0.85 * fade;
	}

	private resetRevealState(): void {
		this.reveal = null;
		this.revealActiveUniform.value = 0;
		this.revealProgressUniform.value = 1;
		this.revealMaxRadiusUniform.value = 1;
		if (this.linkSegments && this.linkMaterial) this.linkSegments.material = this.linkMaterial;
		if (this.selSegments && this.selMaterial) this.selSegments.material = this.selMaterial;
		if (this.ghostMaterial) this.ghostMaterial.opacity = 0.22;
	}

	get revealing(): boolean {
		return this.reveal !== null;
	}

	/** Recompute the colours on a palette or visual-direction change (coordinates untouched) */
	recolor(): void {
		if (!this.nodeGeometry || !this.linkGeometry) return;
		const n = this.data.nodes.length;
		const nodeColAttr = this.nodeGeometry.getAttribute('color') as BufferAttribute;
		const nodeCol = nodeColAttr.array as Float32Array;
		const resolved: Color[] = new Array<Color>(n);
		const hsl = { h: 0, s: 0, l: 0 };
		for (let i = 0; i < n; i++) {
			const node = this.data.nodes[i];
			if (!node) continue;
			let c = this.colorFn(node).clone();
			if (this.tokens.nodeLightness !== null) {
				c.getHSL(hsl);
				c = c.setHSL(hsl.h, hsl.s * 0.95, this.tokens.nodeLightness);
			}
			resolved[i] = c;
			nodeCol[i * 3] = c.r;
			nodeCol[i * 3 + 1] = c.g;
			nodeCol[i * 3 + 2] = c.b;
		}
		nodeColAttr.needsUpdate = true;

		const linkColAttr = this.linkGeometry.getAttribute('color') as BufferAttribute;
		const linkCol = linkColAttr.array as Float32Array;
		const ink = this.tokens.linkInk ? new Color(this.tokens.linkInk) : null;
		const fallback = new Color('#7a87a8');
		const vertsPerLink = this.linkK * 2;
		for (let li = 0; li < this.data.links.length; li++) {
			const l = this.data.links[li];
			if (!l) continue;
			const c = ink ?? linkColor(resolved[l.source] ?? fallback, resolved[l.target] ?? fallback);
			for (let v = 0; v < vertsPerLink; v++) {
				linkCol[(li * vertsPerLink + v) * 3] = c.r;
				linkCol[(li * vertsPerLink + v) * 3 + 1] = c.g;
				linkCol[(li * vertsPerLink + v) * 3 + 2] = c.b;
			}
		}
		linkColAttr.needsUpdate = true;
		// The cluster clouds follow the node colours (kept in step on a theme change or a focus recolour)
		this.clouds?.recolor((i) => {
			const nd = this.data.nodes[i];
			return nd ? this.colorFn(nd) : fallback;
		});
	}

	/** Fit the physics coordinates into CPU display coordinates; the GPU target buffer is synced explicitly by updatePositions. */
	private fitPositions(): void {
		fitGraphPositions(
			this.positions,
			this.renderPositions,
			this.data.nodes.length,
			this.graphRadiusEstimate * GRAPH_FIT_RADIUS_FACTOR,
			this.fitWeights,
		);
	}

	/** Called every frame while the layout is hot: first draw the link-dense body into a centred shell, then gather the links by index */
	updatePositions(): void {
		if (!this.nodeGeometry || !this.linkGeometry) return;
		if (this.reveal) return; // this round's target snapshot is immutable; it catches up to the Worker's latest coordinates in one go at the end
		this.fitPositions();
		const nodeAttr = this.nodeGeometry.getAttribute('position') as BufferAttribute;
		this.nodeTargetPositions.set(this.renderPositions);
		nodeAttr.needsUpdate = true;

		const linkAttr = this.linkGeometry.getAttribute('position') as BufferAttribute;
		fillLinkPositions(linkAttr.array as Float32Array, this.renderPositions, this.data.links, this.linkK, this.linkCurvature);
		linkAttr.needsUpdate = true;
		this.updateSelPositions();
		this.updateGhostPositions();
	}

	// ---------- focus and selection highlighting ----------

	/**
	 * Focus mode: fade or brighten per node by weight.  weightOf returns the aDim target (1 = full, 0.12 = faded out);
	 * graded selection (selected / first degree / second degree / the rest) is just different weights, so a single float aDim suffices.
	 */
	setFocus(weightOf: ((i: number) => number) | null): void {
		const n = this.data.nodes.length;
		this.focusActive = weightOf !== null;
		for (let i = 0; i < n; i++) {
			this.dimTarget[i] = weightOf ? weightOf(i) : 1;
		}
		this.dimAnimating = true;
		this.syncLinkOpacity();
	}

	/** Selected-link highlighting: tier1 = first degree (full saturation), tier2 = second degree (dimmer); the same layer is reused, adding zero draw calls */
	setSelectedLinks(tier1: number[], tier2: number[]): void {
		this.selTier1 = tier1;
		this.selTier2 = tier2;
		this.buildSelLayer();
	}

	private buildSelLayer(): void {
		this.selLinkIdx = [...this.selTier1, ...this.selTier2];
		this.selLinks = this.selLinkIdx.map((i) => this.data.links[i]);
		if (this.selSegments) {
			this.scene.remove(this.selSegments);
			this.selGeometry?.dispose();
			this.selMaterial?.dispose();
			this.selSegments = null;
			this.selGeometry = null;
			this.selMaterial = null;
		}
		const m = this.selLinkIdx.length;
		if (m === 0) return;
		const t1 = this.selTier1.length;
		const K = this.linkK; // the highlight layer matches the main link layer's curvature and segment count: the arcs coincide exactly
		const vertsPerLink = K * 2;
		const pos = new Float32Array(m * vertsPerLink * 3);
		const col = new Float32Array(m * vertsPerLink * 3);
		const hsl = { h: 0, s: 0, l: 0 };
		for (let k = 0; k < m; k++) {
			const l = this.data.links[this.selLinkIdx[k] ?? -1];
			if (!l) continue;
			const sNode = this.data.nodes[l.source];
			const c = sNode ? this.colorFn(sNode).clone() : new Color('#9aa4b2');
			c.getHSL(hsl);
			const isT2 = k >= t1;
			const light = this.tokens.lightMode ? (isT2 ? 0.34 : 0.42) : isT2 ? 0.46 : 0.62;
			const sat = isT2 ? hsl.s * 0.9 : Math.min(hsl.s * 1.2, 1);
			c.setHSL(hsl.h, sat, light);
			const dim = isT2 ? 0.55 : 1; // the second degree is dimmed further, to read as an outer shell
			for (let v = 0; v < vertsPerLink; v++) {
				col[(k * vertsPerLink + v) * 3] = c.r * dim;
				col[(k * vertsPerLink + v) * 3 + 1] = c.g * dim;
				col[(k * vertsPerLink + v) * 3 + 2] = c.b * dim;
			}
		}
		this.selGeometry = new BufferGeometry();
		this.selGeometry.setAttribute('position', new BufferAttribute(pos, 3));
		this.selGeometry.setAttribute('color', new BufferAttribute(col, 3));
		this.selMaterial = new LineBasicMaterial({
			vertexColors: true,
			transparent: true,
			opacity: 0.85,
			depthWrite: false,
		});
		this.selSegments = new LineSegments(this.selGeometry, this.selMaterial);
		this.selSegments.renderOrder = 2;
		this.selSegments.frustumCulled = false;
		this.scene.add(this.selSegments);
		this.updateSelPositions();
		if (this.reveal) this.activateRevealSelectionLayer(this.reveal.target);
	}

	private updateSelPositions(): void {
		if (!this.selGeometry || this.selLinks.length === 0) return;
		const attr = this.selGeometry.getAttribute('position') as BufferAttribute;
		fillLinkPositions(attr.array as Float32Array, this.renderPositions, this.selLinks, this.linkK, this.linkCurvature);
		attr.needsUpdate = true;
	}

	private effectiveLinkOpacity(): number {
		const base = this.baseLinkOpacity * this.tokens.linkOpacityScale;
		return this.focusActive ? base * 0.25 : base;
	}

	private syncLinkOpacity(): void {
		const opacity = this.effectiveLinkOpacity();
		if (this.linkMaterial) this.linkMaterial.opacity = opacity;
		if (this.reveal) this.syncRevealLinkOpacity(this.revealProgressUniform.value);
		else if (this.revealLinkMaterial) this.revealLinkMaterial.opacity = opacity;
	}

	// ---------- the visual direction ----------

	applyTokens(tokens: VisualTokens, bloomStrengthFromSettings: number): void {
		this.tokens = tokens;
		this.scene.background = new Color(tokens.background);
		this.starfield.visible = tokens.starfield && this.starfieldEnabled;
		this.syncSpace(); // daylight forces the background shape layers off
		this.renderer.toneMapping = tokens.lightMode ? NoToneMapping : ACESFilmicToneMapping;
		if (this.nodeMaterial) {
			this.nodeMaterial.uniforms['uLightMode']!.value = tokens.lightMode ? 1 : 0;
		}
		this.bloomPass.enabled = tokens.bloomEnabled && this.tierBloomAllowed && bloomStrengthFromSettings > 0.001;
		if (tokens.motes && !this.motes) this.buildMotes();
		if (this.motes) this.motes.visible = tokens.motes;
		this.syncLinkOpacity();
		this.recolor();
		this.buildSelLayer();
	}

	get currentTokens(): VisualTokens {
		return this.tokens;
	}

	// ---------- deep-space background shape layers (v0.4: the nebula backdrop / floating field stars / cluster clouds) ----------

	/** Each of the three layers has its own switch (0 = off); actual visibility = the slider × tokens.space × the quality tier */
	setSpace(space: SpaceSettings): void {
		this.space = { ...space };
		this.syncSpace();
	}

	private syncSpace(): void {
		const allowed = this.tokens.space;
		// The nebula backdrop: built and baked only on first enabling (a one-off, tens of ms); after that the strength only adjusts opacity
		const nebulaV = allowed ? this.space.nebula : 0;
		if (nebulaV > 0.005 && !this.nebula) {
			// The volumetric cloud radius = the graph radius × 2.6: the clouds wrap the graph with near and far parts, rather than sticking to a shell at infinity
			this.nebula = new NebulaDome(this.graphRadiusEstimate * 2.6);
			this.nebula.setQuality(this.tierStarScale);
			this.nebula.setPixelScale(this.pixelScale, 1600 * this.renderer.getPixelRatio());
			this.nebula.bake(this.nebulaTintA, this.nebulaTintB);
			this.scene.add(this.nebula.object);
		}
		this.nebula?.setIntensity(nebulaV);
		// Floating field stars: rebuilt only on a density or tier change (≤1200 points, milliseconds)
		const density = allowed ? this.space.fieldStars : 0;
		if (Math.abs(density - this.fieldStarsDensity) > 1e-3) {
			this.disposeFieldStars();
			if (density > 0.005) {
				this.fieldStars = buildFieldStars(this.graphRadiusEstimate * 2.4, density, this.tierStarScale);
				this.scene.add(this.fieldStars);
			}
			this.fieldStarsDensity = density;
		}
		// Cluster clouds: off on the mobile tier (the fill-rate risk of large additive sprites)
		const cloudsV = allowed && this.tierCloudsAllowed ? this.space.clusterClouds : 0;
		if (cloudsV > 0.005 && !this.clouds) {
			this.clouds = new ClusterClouds();
			this.clouds.setPixelScale(this.pixelScale, 300 * this.renderer.getPixelRatio());
			this.scene.add(this.clouds.points);
			this.refreshClusterClouds(); // clouds appear at once when the warm-start coordinates are already in place; otherwise they wait for settling
		}
		this.clouds?.setIntensity(cloudsV);
	}

	/** The nebula tint follows the colour theme (the first two groups' colours); re-baked only on a theme change */
	setNebulaTint(hexA: string, hexB: string): void {
		if (hexA === this.nebulaTintA && hexB === this.nebulaTintB) return;
		this.nebulaTintA = hexA;
		this.nebulaTintB = hexB;
		this.nebula?.bake(hexA, hexB);
	}

	/** Called by GraphController at the settling moment: recompute the cluster centroids and spread, and recolour */
	refreshClusterClouds(): void {
		if (!this.clouds || this.data.nodes.length === 0) return;
		this.clouds.rebuild(this.data, this.reveal?.target ?? this.renderPositions, this.graphRadiusEstimate);
		const fallback = new Color('#7a87a8');
		this.clouds.recolor((i) => {
			const nd = this.data.nodes[i];
			return nd ? this.colorFn(nd) : fallback;
		});
		this.clouds.setIntensity(this.tokens.space && this.tierCloudsAllowed ? this.space.clusterClouds : 0);
	}

	private disposeFieldStars(): void {
		if (!this.fieldStars) return;
		this.fieldStars.geometry.dispose();
		this.fieldStars.material.dispose();
		this.scene.remove(this.fieldStars);
		this.fieldStars = null;
	}

	/** Daylight mode's dust motes: 600 points, larger near and smaller far, drifting slowly */
	private buildMotes(): void {
		const count = 600;
		const pos = new Float32Array(count * 3);
		const R = this.graphRadiusEstimate * 2.2;
		for (let i = 0; i < count; i++) {
			pos[i * 3] = (Math.random() * 2 - 1) * R;
			pos[i * 3 + 1] = (Math.random() * 2 - 1) * R;
			pos[i * 3 + 2] = (Math.random() * 2 - 1) * R;
		}
		const geo = new BufferGeometry();
		geo.setAttribute('position', new BufferAttribute(pos, 3));
		const mat = new PointsMaterial({
			color: new Color('#d8d4cb'),
			size: 1.6,
			sizeAttenuation: true,
			transparent: true,
			opacity: 0.5,
			depthWrite: false,
		});
		this.motes = new Points(geo, mat);
		this.motes.renderOrder = -1;
		this.scene.add(this.motes);
	}

	// ---------- the render loop ----------

	render(deltaS: number, animationDeltaS = deltaS): void {
		this.starfield.rotation.y += STARFIELD_ROTATION_RAD_PER_S * deltaS;
		if (this.starfield.visible) this.twinkler.update(deltaS, this.twinkleFreq);
		if (this.nebula?.visible) this.nebula.update(deltaS);
		if (this.fieldStars) this.fieldStars.rotation.y -= STARFIELD_ROTATION_RAD_PER_S * 0.6 * deltaS; // a slow counter-rotation = parallax
		if (this.motes?.visible) this.motes.rotation.y -= STARFIELD_ROTATION_RAD_PER_S * 2 * deltaS;
		if (this.dimAnimating) this.stepDim(deltaS);
		if (this.reveal) this.stepReveal(animationDeltaS);
		this.renderer.info.reset();
		this.composer.render();
	}

	private stepDim(deltaS: number): void {
		const k = Math.min(deltaS / FOCUS_FADE_S, 1);
		let active = false;
		for (let i = 0; i < this.dimCurrent.length; i++) {
			const cur = this.dimCurrent[i] ?? 1;
			const tgt = this.dimTarget[i] ?? 1;
			const next = cur + (tgt - cur) * k;
			this.dimCurrent[i] = Math.abs(next - tgt) < 0.005 ? tgt : next;
			if (this.dimCurrent[i] !== tgt) active = true;
		}
		this.dimAnimating = active;
		if (this.nodeGeometry) {
			(this.nodeGeometry.getAttribute('aDim') as BufferAttribute).needsUpdate = true;
		}
	}

	get drawCalls(): number {
		return this.renderer.info.render.calls;
	}

	// ---------- parameters ----------

	setBloomParams(p: { strength: number; radius: number; threshold: number }): void {
		this.bloomPass.strength = p.strength;
		this.bloomPass.radius = p.radius;
		this.bloomPass.threshold = p.threshold;
		this.bloomPass.enabled = this.tokens.bloomEnabled && this.tierBloomAllowed && p.strength > 0.001;
	}

	getBloomStrength(): number {
		return this.bloomPass.enabled ? this.bloomPass.strength : 0;
	}

	setBloomStrength(v: number): void {
		this.bloomPass.strength = v;
		this.bloomPass.enabled = this.tokens.bloomEnabled && this.tierBloomAllowed && v > 0.001;
	}

	/** The quality tier (M4): pixelRatio / the bloom gate / starfield density / curve segments / the background layers' budget */
	applyTier(tier: QualityTier, bloomStrengthFromSettings: number): void {
		this.tierBloomAllowed = tier.bloomAllowed;
		const pixelRatio = effectivePixelRatio(window.devicePixelRatio, tier.pixelRatioCap);
		this.renderer.setPixelRatio(pixelRatio);
		this.composer.setPixelRatio(pixelRatio);
		this.bloomPass.enabled = this.tokens.bloomEnabled && this.tierBloomAllowed && bloomStrengthFromSettings > 0.001;
		// The starfield is rebuilt at the tier's density (a one-off, milliseconds)
		const visible = this.starfield.visible;
		const rotation = this.starfield.rotation.y;
		disposeStarfield(this.starfield);
		this.scene.remove(this.starfield);
		const sf = buildStarfield(this.graphRadiusEstimate * 6.5, tier.starScale);
		this.starfield = sf.group;
		this.twinkler = sf.twinkler;
		this.starfield.visible = visible;
		this.starfield.rotation.y = rotation;
		this.scene.add(this.starfield);
		// Changing the curve segment count: a geometry rebuild + recolour + a highlight-layer rebuild (a one-off)
		this.tierLinkSegs = tier.linkSegments;
		if (this.linkGeometry && segsFor(this.linkCurvature, this.tierLinkSegs) !== this.linkK) {
			this.buildLinkLayer();
			this.recolor();
			this.buildSelLayer();
			if (this.reveal) this.activateRevealLinkLayer(this.reveal.target);
			else this.updatePositions();
		}
		// The background layers' budget: nebula sprite density / field-star density scaling / the cloud switch
		this.tierCloudsAllowed = tier.clusterCloudsAllowed;
		if (this.tierStarScale !== tier.starScale) {
			this.tierStarScale = tier.starScale;
			if (this.nebula) {
				this.nebula.setQuality(tier.starScale);
				this.nebula.bake(this.nebulaTintA, this.nebulaTintB); // regenerate the clouds at the new density
			}
		}
		this.fieldStarsDensity = -1; // force a field-star rebuild at the new tier's scaling
		this.syncSpace();
		this.resize(this.lastW, this.lastH); // a pixelRatio change → recompute uPixelScale/uMaxPoint and the buffer sizes
		const u = this.nodeMaterial?.uniforms['uMaxPoint'];
		if (u) u.value = 110 * this.renderer.getPixelRatio();
	}

	setLinkOpacity(v: number): void {
		this.baseLinkOpacity = v;
		this.syncLinkOpacity();
	}

	setNodeScale(v: number): void {
		this.nodeScale = v;
		const u = this.nodeMaterial?.uniforms['uSizeMul'];
		if (u) u.value = v;
	}

	/** The starfield background switch (a user option); in deep-space mode it has the final say on starfield visibility */
	setStarfieldEnabled(on: boolean): void {
		this.starfieldEnabled = on;
		this.starfield.visible = on && this.tokens.starfield;
	}

	resize(w: number, h: number): void {
		if (w < 2 || h < 2) return;
		this.lastW = w;
		this.lastH = h;
		this.camera.aspect = w / h;
		this.camera.updateProjectionMatrix();
		this.renderer.setSize(w, h);
		this.composer.setSize(w, h);
		this.bloomPass.resolution.set(w, h);
		const physH = h * this.renderer.getPixelRatio();
		this.pixelScale = physH / (2 * Math.tan(((this.camera.fov / 2) * Math.PI) / 180));
		const u = this.nodeMaterial?.uniforms['uPixelScale'];
		if (u) u.value = this.pixelScale;
		this.clouds?.setPixelScale(this.pixelScale, 300 * this.renderer.getPixelRatio());
		this.nebula?.setPixelScale(this.pixelScale, 1600 * this.renderer.getPixelRatio());
	}

	// ---------- picking and projection ----------

	/** The same radial expansion as the vertex shader; computed only for the nodes actually queried, never as an O(N) scan per frame. */
	private displayPosition(i: number, out: Vector3): Vector3 {
		const reveal = this.reveal;
		const positions = reveal?.target ?? this.renderPositions;
		const x = positions[i * 3] ?? 0;
		const y = positions[i * 3 + 1] ?? 0;
		const z = positions[i * 3 + 2] ?? 0;
		if (!reveal) return out.set(x, y, z);
		const scale = revealScale(this.revealProgressUniform.value, Math.hypot(x, y, z), reveal.maxR);
		return out.set(x * scale, y * scale, z * scale);
	}

	/** Project to logical screen pixels; z>1 = behind the camera */
	projectNode(i: number, w: number, h: number): { x: number; y: number; behind: boolean } {
		this.displayPosition(i, this.projVec);
		this.projVec.project(this.camera);
		return {
			x: ((this.projVec.x + 1) / 2) * w,
			y: ((1 - this.projVec.y) / 2) * h,
			behind: this.projVec.z > 1,
		};
	}

	/** Screen-space nearest-neighbour picking (the O(n) runs only on a click or a throttled hover) */
	pickNearest(px: number, py: number, w: number, h: number, maxPx: number): number {
		let best = -1;
		let bestDist = maxPx;
		for (let i = 0; i < this.data.nodes.length; i++) {
			const p = this.projectNode(i, w, h);
			if (p.behind) continue;
			const d = Math.hypot(p.x - px, p.y - py);
			if (d < bestDist) {
				bestDist = d;
				best = i;
			}
		}
		return best;
	}

	/**
	 * Picks **the dominant community** around the cursor.  What reads on screen as "this clump".
	 *
	 * Why pickNearest cannot stand in —— picking the single nearest node picks the sparsely
	 * scattered 'other' more and more as the radius widens.  Measured (7,893 entities, 1728x811):
	 *
	 *     radius 10px → 42% hit rate · 34% of those 'other'
	 *     radius 64px → 65% hit rate · 50% of those 'other'
	 *
	 * That is, a 6× radius gives 1.5× the hit rate and half the increase is the junk bin.
	 * The community a person sees is not the nearest node but **the colour most densely laid
	 * around it**, so it counts the nodes inside the radius by community, weighting the near ones heavily.
	 *
	 * Nodes for which groupOf returns null are not counted —— which communities are candidates is
	 * the caller's decision (the renderer need not know what 'other' is).
	 */
	pickGroupAt(
		px: number, py: number, w: number, h: number, maxPx: number,
		groupOf: (i: number) => string | null,
	): string | null {
		const score = new Map<string, number>();
		for (let i = 0; i < this.data.nodes.length; i++) {
			const p = this.projectNode(i, w, h);
			if (p.behind) continue;
			const d = Math.hypot(p.x - px, p.y - py);
			if (d > maxPx) continue;
			const g = groupOf(i);
			if (g === null) continue;
			// 1/(1+d): directly under the cursor weighs more than the edge.  With linear falloff,
			// several nodes at the rim beat the single one at the centre and the cursor jitters.
			score.set(g, (score.get(g) ?? 0) + 1 / (1 + d));
		}
		let best: string | null = null;
		let bestScore = 0;
		for (const [g, sc] of score) {
			if (sc > bestScore) { bestScore = sc; best = g; }
		}
		return best;
	}

	nodeRadius(i: number): number {
		return this.sizes[i] ?? NODE_BASE_RADIUS;
	}

	nodePosition(i: number, out: Vector3): Vector3 {
		return this.displayPosition(i, out);
	}

	nodeColorHex(i: number): string {
		const node = this.data.nodes[i];
		return node ? `#${this.colorFn(node).getHexString()}` : '#9aa4b2';
	}

	cameraDistanceTo(i: number): number {
		this.displayPosition(i, this.projVec);
		return this.camera.position.distanceTo(this.projVec);
	}

	// ---------- the destruction contract ----------

	private disposeGraphObjects(): void {
		if (this.nodePoints) this.scene.remove(this.nodePoints);
		if (this.linkSegments) this.scene.remove(this.linkSegments);
		if (this.selSegments) {
			this.scene.remove(this.selSegments);
			this.selGeometry?.dispose();
			this.selMaterial?.dispose();
			this.selSegments = null;
			this.selGeometry = null;
			this.selMaterial = null;
		}
		if (this.ghostSegments) {
			this.scene.remove(this.ghostSegments);
			this.ghostGeometry?.dispose();
			this.ghostMaterial?.dispose();
			this.ghostSegments = null;
			this.ghostGeometry = null;
			this.ghostMaterial = null;
		}
		this.nodeGeometry?.dispose();
		this.nodeMaterial?.dispose();
		this.linkGeometry?.dispose();
		this.linkMaterial?.dispose();
		this.revealLinkMaterial?.dispose();
		this.revealSelMaterial?.dispose();
		this.nodePoints = null;
		this.linkSegments = null;
		this.nodeGeometry = null;
		this.linkGeometry = null;
		this.nodeMaterial = null;
		this.linkMaterial = null;
		this.revealLinkMaterial = null;
		this.revealSelMaterial = null;
	}

	/** The destruction contract: composer targets → scene resources → renderer → force-lose the context */
	dispose(): void {
		this.disposeGraphObjects();
		disposeStarfield(this.starfield);
		this.scene.remove(this.starfield);
		this.disposeFieldStars();
		if (this.nebula) {
			this.nebula.dispose();
			this.scene.remove(this.nebula.object);
			this.nebula = null;
		}
		if (this.clouds) {
			this.clouds.dispose();
			this.scene.remove(this.clouds.points);
			this.clouds = null;
		}
		if (this.motes) {
			this.motes.geometry.dispose();
			(this.motes.material as PointsMaterial).dispose();
			this.scene.remove(this.motes);
			this.motes = null;
		}
		this.bloomPass.dispose();
		this.outputPass.dispose();
		this.renderPass.dispose();
		this.composer.dispose();
		this.renderer.dispose();
		try {
			this.renderer.forceContextLoss();
		} catch {
			// The context may already be lost
		}
		this.renderer.domElement.remove();
	}
}
