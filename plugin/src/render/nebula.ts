import {
	AdditiveBlending,
	BufferAttribute,
	BufferGeometry,
	Color,
	Points,
	ShaderMaterial,
} from 'three';
import type { GraphData } from '../types';

// ---------- shared: deterministic noise / random ----------

function hash2(ix: number, iy: number, seed: number): number {
	let h = (ix * 374761393 + iy * 668265263 + seed * 1442695) | 0;
	h = Math.imul(h ^ (h >>> 13), 1274126177);
	return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

/** mulberry32: a deterministic PRNG; a fixed seed → the same distribution every time (a theme change only recolours, it does not move the clouds) */
function rng(seed: number): () => number {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

const NEBULA_ROTATION_RAD_PER_S = 0.0004; // an extremely slow rotation = the whole cloud mass breathing

// Volumetric clouds: large soft-Gaussian sprites facing the camera, with sizeAttenuation for near-large/far-small → real depth, additively blended into density.
const NEBULA_VERTEX = /* glsl */ `
attribute float aSize;
attribute vec3 aColor;
varying vec3 vColor;
uniform float uPixelScale;
uniform float uMaxPoint;

void main() {
	vColor = aColor;
	vec4 mv = modelViewMatrix * vec4(position, 1.0);
	gl_PointSize = min(aSize * uPixelScale / max(-mv.z, 1.0), uMaxPoint);
	gl_Position = projectionMatrix * mv;
}
`;

const NEBULA_FRAGMENT = /* glsl */ `
varying vec3 vColor;
uniform float uIntensity;

void main() {
	// r: 0 = the centre → 1 = the sprite radius; a very soft Gaussian, so adjacent cloud sheets merge seamlessly into fog (rather than individual round blobs)
	float r = length(gl_PointCoord - 0.5) * 2.0;
	float a = exp(-r * r * 2.6) - 0.0743; // exactly zero at r=1; a larger coefficient = a less solid centre and airier edges
	if (a < 0.003) discard;
	// The coefficients are pushed down to "thin as mist": colour 0.30 / opacity 0.22 —— dense areas still take colour under additive blending, but nothing reads as a solid block
	gl_FragColor = vec4(vColor * uIntensity * 0.30, a * uIntensity * 0.22);
}
`;

const NEBULA_CENTERS = 6; // the number of cloud cores: a few clumps at different depths around the graph
const SPRITES_PER_CENTER = 24;
const NEBULA_MAX = NEBULA_CENTERS * SPRITES_PER_CENTER;

/**
 * The nebula (volumetric version, v0.4.1): the BackSide sphere-shell backdrop was abandoned (Rick's feedback, "like a sphere wrapped in mid-air"),
 * replaced by soft billboard cloud sheets scattered through the volume around the graph.  The cores clump at different depths, additive blending gives natural density,
 * and sizeAttenuation gives parallax while cruising.  Always facing the camera = the sphere's outline never shows.
 * The distribution uses a fixed seed (a theme change only recolours, it does not move the clouds); the strength slider adjusts a uniform alone (zero rebuild).
 */
export class NebulaDome {
	readonly object: Points<BufferGeometry, ShaderMaterial>;
	private radius: number;
	private scale = 1; // the quality tier's density scaling (the sprite count)
	private count = 0;

	constructor(radius: number) {
		this.radius = radius;
		const geo = new BufferGeometry();
		geo.setAttribute('position', new BufferAttribute(new Float32Array(NEBULA_MAX * 3), 3));
		geo.setAttribute('aColor', new BufferAttribute(new Float32Array(NEBULA_MAX * 3), 3));
		geo.setAttribute('aSize', new BufferAttribute(new Float32Array(NEBULA_MAX), 1));
		geo.setDrawRange(0, 0);
		const mat = new ShaderMaterial({
			vertexShader: NEBULA_VERTEX,
			fragmentShader: NEBULA_FRAGMENT,
			transparent: true,
			depthWrite: false,
			blending: AdditiveBlending,
			uniforms: {
				uPixelScale: { value: 1 },
				uMaxPoint: { value: 1600 },
				uIntensity: { value: 0 },
			},
		});
		this.object = new Points(geo, mat);
		this.object.renderOrder = -1; // above the starfield backdrop, below the nodes and links (nodes at renderOrder 1 always cover it)
		this.object.frustumCulled = false;
	}

	setQuality(scale: number): void {
		this.scale = scale;
	}

	/** Generate or regenerate the clouds: positions from a fixed seed, colours taking the theme's two colours (each core one step between them + a perturbation) */
	bake(tintHexA: string, tintHexB: string): void {
		const rand = rng(0x9e37);
		const posAttr = this.object.geometry.getAttribute('position') as BufferAttribute;
		const colAttr = this.object.geometry.getAttribute('aColor') as BufferAttribute;
		const sizeAttr = this.object.geometry.getAttribute('aSize') as BufferAttribute;
		const pos = posAttr.array as Float32Array;
		const col = colAttr.array as Float32Array;
		const size = sizeAttr.array as Float32Array;
		const R = this.radius;
		const a = new Color(tintHexA);
		const b = new Color(tintHexB);
		const hsl = { h: 0, s: 0, l: 0 };
		const c = new Color();
		const centers = Math.max(2, Math.round(NEBULA_CENTERS * this.scale));
		const per = Math.max(6, Math.round(SPRITES_PER_CENTER * this.scale));
		let p = 0;
		for (let ci = 0; ci < centers && p < NEBULA_MAX; ci++) {
			// The core direction is random (isotropic), the depth 0.7R..2.4R → some near, some far, wrapping the graph
			const theta = 2 * Math.PI * rand();
			const phi = Math.acos(2 * rand() - 1);
			const cr = R * (0.7 + 1.7 * rand());
			const cx = cr * Math.sin(phi) * Math.cos(theta);
			const cy = cr * Math.sin(phi) * Math.sin(theta) * 0.75; // slightly flattened, so the cloud band leans towards a disc
			const cz = cr * Math.cos(phi);
			const spread = R * (0.5 + 0.5 * rand());
			// The core's colour: one step between the two theme colours + a lowered brightness (additive blending naturally lifts the dense areas)
			c.copy(a).lerp(b, rand());
			c.getHSL(hsl);
			c.setHSL(hsl.h + (rand() - 0.5) * 0.05, Math.min(hsl.s * 0.85, 0.9), 0.16 + 0.08 * rand());
			for (let k = 0; k < per && p < NEBULA_MAX; k++) {
				// An approximate normal offset (the sum of 3 uniforms) → dense at the centre, thin at the edges
				const gx = (rand() + rand() + rand() - 1.5) * spread;
				const gy = (rand() + rand() + rand() - 1.5) * spread * 0.8;
				const gz = (rand() + rand() + rand() - 1.5) * spread;
				pos[p * 3] = cx + gx;
				pos[p * 3 + 1] = cy + gy;
				pos[p * 3 + 2] = cz + gz;
				// Size: mostly medium with a few large (the big sheets lay the base, the small ones break it up)
				const big = rand() < 0.25;
				size[p] = R * (big ? 1.0 + 0.8 * rand() : 0.4 + 0.5 * rand());
				const j = 0.85 + 0.3 * rand(); // a slight brightness perturbation per sheet
				col[p * 3] = c.r * j;
				col[p * 3 + 1] = c.g * j;
				col[p * 3 + 2] = c.b * j;
				p++;
			}
		}
		this.count = p;
		posAttr.needsUpdate = true;
		colAttr.needsUpdate = true;
		sizeAttr.needsUpdate = true;
		this.object.geometry.setDrawRange(0, p);
	}

	setPixelScale(pixelScale: number, maxPointPx: number): void {
		this.object.material.uniforms['uPixelScale']!.value = pixelScale;
		this.object.material.uniforms['uMaxPoint']!.value = maxPointPx;
	}

	/** Strength 0–1 (no rebuild, the slider is instant) */
	setIntensity(v: number): void {
		this.object.material.uniforms['uIntensity']!.value = v;
		this.object.visible = v > 0.005 && this.count > 0;
	}

	get visible(): boolean {
		return this.object.visible;
	}

	update(deltaS: number): void {
		this.object.rotation.y += NEBULA_ROTATION_RAD_PER_S * deltaS;
	}

	dispose(): void {
		this.object.geometry.dispose();
		this.object.material.dispose();
	}
}

// ---------- cluster clouds ----------

const CLOUD_VERTEX = /* glsl */ `
attribute float aSize;
varying vec3 vColor;
uniform float uPixelScale;
uniform float uMaxPoint;

void main() {
	vColor = color;
	vec4 mv = modelViewMatrix * vec4(position, 1.0);
	gl_PointSize = min(aSize * uPixelScale / max(-mv.z, 1.0), uMaxPoint);
	gl_Position = projectionMatrix * mv;
}
`;

const CLOUD_FRAGMENT = /* glsl */ `
varying vec3 vColor;
uniform float uIntensity;

void main() {
	vec2 uv = gl_PointCoord - 0.5;
	float d2 = dot(uv, uv);
	float a = exp(-d2 * 10.0) - 0.0821; // a soft Gaussian, exactly zero at r=0.5 (no square hard edge)
	if (a < 0.004) discard;
	gl_FragColor = vec4(vColor * uIntensity * 0.55, a * uIntensity * 0.4);
}
`;

const MAX_CLUSTERS = 10;
const POINTS_PER_CLUSTER = 3;

/**
 * Cluster clouds: coloured clouds over dense star clusters (the "wreathed in mist" subject of the reference image).
 * The top nodes by degree become seeds, kept apart greedily → each cluster's centroid and spread radius → 3 jittered soft sprites,
 * with the colour = the cluster's mean node colour, saturated.  Every cloud in 1 draw call; the member indices are cached for recolouring on a theme change.
 * Recomputed only at the layout's settling moment (driven by GraphController.checkSettled), at zero cost while cruising.
 */
export class ClusterClouds {
	readonly points: Points<BufferGeometry, ShaderMaterial>;
	private memberSamples: number[][] = [];
	private count = 0;

	constructor() {
		const geo = new BufferGeometry();
		geo.setAttribute('position', new BufferAttribute(new Float32Array(MAX_CLUSTERS * POINTS_PER_CLUSTER * 3), 3));
		geo.setAttribute('color', new BufferAttribute(new Float32Array(MAX_CLUSTERS * POINTS_PER_CLUSTER * 3), 3));
		geo.setAttribute('aSize', new BufferAttribute(new Float32Array(MAX_CLUSTERS * POINTS_PER_CLUSTER), 1));
		geo.setDrawRange(0, 0);
		const mat = new ShaderMaterial({
			vertexShader: CLOUD_VERTEX,
			fragmentShader: CLOUD_FRAGMENT,
			vertexColors: true,
			transparent: true,
			depthWrite: false,
			blending: AdditiveBlending,
			uniforms: {
				uPixelScale: { value: 1 },
				uMaxPoint: { value: 300 },
				uIntensity: { value: 0 },
			},
		});
		this.points = new Points(geo, mat);
		this.points.renderOrder = -1; // the same layer as the starfield (added after it, so drawn above the star points and below the links)
		this.points.frustumCulled = false;
	}

	/** Recompute the clusters and geometry at the settling moment; the member indices go stale after a data rebuild, so clear() must be called before calling this again */
	rebuild(data: GraphData, positions: Float32Array, graphRadius: number): void {
		const nodes = data.nodes;
		const n = nodes.length;
		this.memberSamples = [];
		if (n < 20) {
			this.count = 0;
			this.points.geometry.setDrawRange(0, 0);
			return;
		}
		// Seeds: the top candidates by degree + greedy spacing (≥0.5R), at most MAX_CLUSTERS clusters
		const order = Array.from({ length: n }, (_, i) => i).sort((x, y) => (nodes[y]?.degree ?? 0) - (nodes[x]?.degree ?? 0));
		const seeds: number[] = [];
		const minGap = graphRadius * 0.5;
		for (const cand of order.slice(0, 250)) {
			const cx = positions[cand * 3] ?? 0;
			const cy = positions[cand * 3 + 1] ?? 0;
			const cz = positions[cand * 3 + 2] ?? 0;
			let ok = true;
			for (const sd of seeds) {
				if (Math.hypot(cx - (positions[sd * 3] ?? 0), cy - (positions[sd * 3 + 1] ?? 0), cz - (positions[sd * 3 + 2] ?? 0)) < minGap) {
					ok = false;
					break;
				}
			}
			if (ok) seeds.push(cand);
			if (seeds.length >= MAX_CLUSTERS) break;
		}
		const posAttr = this.points.geometry.getAttribute('position') as BufferAttribute;
		const sizeAttr = this.points.geometry.getAttribute('aSize') as BufferAttribute;
		const pArr = posAttr.array as Float32Array;
		const sArr = sizeAttr.array as Float32Array;
		const memberR = graphRadius * 0.33;
		let p = 0;
		let hashSeed = 11;
		for (const sd of seeds) {
			const sx = positions[sd * 3] ?? 0;
			const sy = positions[sd * 3 + 1] ?? 0;
			const sz = positions[sd * 3 + 2] ?? 0;
			// Cluster members: the nodes in the seed's neighbourhood (O(n·clusters), run once at the settling moment only)
			const members: number[] = [];
			let mx = 0;
			let my = 0;
			let mz = 0;
			for (let i = 0; i < n; i++) {
				const dx = (positions[i * 3] ?? 0) - sx;
				const dy = (positions[i * 3 + 1] ?? 0) - sy;
				const dz = (positions[i * 3 + 2] ?? 0) - sz;
				if (dx * dx + dy * dy + dz * dz < memberR * memberR) {
					members.push(i);
					mx += positions[i * 3] ?? 0;
					my += positions[i * 3 + 1] ?? 0;
					mz += positions[i * 3 + 2] ?? 0;
				}
			}
			if (members.length < 8) continue;
			mx /= members.length;
			my /= members.length;
			mz /= members.length;
			let sq = 0;
			for (const i of members) {
				sq += (((positions[i * 3] ?? 0) - mx) ** 2 + ((positions[i * 3 + 1] ?? 0) - my) ** 2 + ((positions[i * 3 + 2] ?? 0) - mz) ** 2);
			}
			const spread = Math.sqrt(sq / members.length);
			const sample = members.length > 120 ? members.filter((_, k) => k % Math.ceil(members.length / 120) === 0) : members;
			for (let k = 0; k < POINTS_PER_CLUSTER; k++) {
				const jx = (hash2(hashSeed, k, 3) - 0.5) * spread;
				const jy = (hash2(hashSeed, k, 5) - 0.5) * spread * 0.7;
				const jz = (hash2(hashSeed, k, 7) - 0.5) * spread;
				pArr[p * 3] = mx + jx;
				pArr[p * 3 + 1] = my + jy;
				pArr[p * 3 + 2] = mz + jz;
				sArr[p] = spread * (1.6 + 0.8 * hash2(hashSeed, k, 13));
				this.memberSamples.push(sample);
				p++;
			}
			hashSeed++;
		}
		this.count = p;
		posAttr.needsUpdate = true;
		sizeAttr.needsUpdate = true;
		this.points.geometry.setDrawRange(0, p);
	}

	/** After a data rebuild (a vault change) the member indices go stale: clear first and recompute at the next settling */
	clear(): void {
		this.count = 0;
		this.memberSamples = [];
		this.points.geometry.setDrawRange(0, 0);
	}

	/** Recolour on a palette-theme change (without recomputing the clusters): the cluster's mean colour → saturated at a fixed brightness, reading as a coloured nebula under additive blending */
	recolor(colorOf: (nodeIndex: number) => Color): void {
		if (this.count === 0) return;
		const colAttr = this.points.geometry.getAttribute('color') as BufferAttribute;
		const cArr = colAttr.array as Float32Array;
		const hsl = { h: 0, s: 0, l: 0 };
		const acc = new Color();
		for (let p = 0; p < this.count; p++) {
			const sample = this.memberSamples[p] ?? [];
			acc.setRGB(0, 0, 0);
			for (const i of sample) acc.add(colorOf(i));
			if (sample.length > 0) acc.multiplyScalar(1 / sample.length);
			acc.getHSL(hsl);
			acc.setHSL(hsl.h, Math.min(hsl.s * 1.25, 1), 0.4);
			cArr[p * 3] = acc.r;
			cArr[p * 3 + 1] = acc.g;
			cArr[p * 3 + 2] = acc.b;
		}
		colAttr.needsUpdate = true;
	}

	setIntensity(v: number): void {
		this.points.material.uniforms['uIntensity']!.value = v;
		this.points.visible = v > 0.005 && this.count > 0;
	}

	get intensity(): number {
		return this.points.material.uniforms['uIntensity']!.value as number;
	}

	setPixelScale(pixelScale: number, maxPointPx: number): void {
		this.points.material.uniforms['uPixelScale']!.value = pixelScale;
		this.points.material.uniforms['uMaxPoint']!.value = maxPointPx;
	}

	dispose(): void {
		this.points.geometry.dispose();
		this.points.material.dispose();
	}
}
