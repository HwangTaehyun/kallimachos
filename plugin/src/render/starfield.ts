import { BufferAttribute, BufferGeometry, Color, Group, Points, PointsMaterial } from 'three';

// The starfield: 3 size classes = 3 draw calls (visual spec §1.2); a shell distribution approximating infinity
const CLASSES = [
	{ count: 2600, size: 1.2 },
	{ count: 900, size: 2.0 },
	{ count: 250, size: 3.0 },
];

const COOL_A = new Color('#9da8c4');
const COOL_B = new Color('#ffffff');
const WARM = new Color('#ffe9c9');
const BLUE = new Color('#bfd3ff');

function mulberry(seed: number): () => number {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

export function disposeStarfield(group: Group): void {
	for (const child of group.children) {
		const p = child as Points<BufferGeometry, PointsMaterial>;
		p.geometry.dispose();
		p.material.dispose();
	}
}

/**
 * Bright-star twinkling (G2.5 feedback).  The taste logic:
 * only the largest size class, the "real stars", twinkle —— background stars do not; at most one at a time;
 * a Poisson random interval + a 1.6s sine envelope —— like a real night sky's occasional atmospheric scintillation, not Christmas lights.
 */
export class Twinkler {
	private baseColors: Float32Array;
	private attr: BufferAttribute;
	private active: { index: number; t: number } | null = null;
	private nextIn = 3;

	constructor(
		private geometry: BufferGeometry,
		private starCount: number,
	) {
		this.attr = geometry.getAttribute('color') as BufferAttribute;
		this.baseColors = new Float32Array(this.attr.array as Float32Array);
	}

	/** freq: the expected twinkles per minute ÷ 10 (the slider is 0–2, 0 = off) */
	update(deltaS: number, freq: number): void {
		if (this.active) {
			this.active.t += deltaS;
			const t = this.active.t;
			const DUR = 1.6;
			const arr = this.attr.array as Float32Array;
			const i = this.active.index * 3;
			const k = t >= DUR ? 1 : 1 + 2.2 * Math.sin((Math.PI * t) / DUR);
			arr[i] = (this.baseColors[i] ?? 1) * k;
			arr[i + 1] = (this.baseColors[i + 1] ?? 1) * k;
			arr[i + 2] = (this.baseColors[i + 2] ?? 1) * k;
			this.attr.needsUpdate = true;
			if (t >= DUR) this.active = null;
			return;
		}
		if (freq <= 0.01) return;
		this.nextIn -= deltaS;
		if (this.nextIn <= 0) {
			this.active = { index: Math.floor(Math.random() * this.starCount), t: 0 };
			// The Poisson interval: a mean of 6/freq seconds (freq=0.5 → about once every 12s)
			this.nextIn = Math.min(Math.max(-Math.log(Math.random() + 1e-9) * (6 / freq), 1.5), 90);
		}
	}
}

/**
 * Floating field stars (a v0.4 background shape layer): sparse small stars scattered inside and outside the graph's volume, with sizeAttenuation for near-large/far-small,
 * producing parallax while cruising —— a different sense of depth from the shell backdrop's "infinitely far" star points.  1 draw call.
 */
export function buildFieldStars(volumeRadius: number, density: number, scale = 1): Points<BufferGeometry, PointsMaterial> {
	const count = Math.max(Math.round(1200 * density * scale), 1);
	const rand = mulberry(0x2f6e1b);
	const pos = new Float32Array(count * 3);
	const col = new Float32Array(count * 3);
	for (let i = 0; i < count; i++) {
		// A cube-root sampling approaches volume uniformity, then the inner 30% is lifted away —— the core region is left to the nodes and the field stars scatter through the mid and far ground
		const r = volumeRadius * (0.3 + 0.7 * Math.cbrt(rand()));
		const theta = 2 * Math.PI * rand();
		const phi = Math.acos(2 * rand() - 1);
		pos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
		pos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
		pos[i * 3 + 2] = r * Math.cos(phi);
		const pick = rand();
		const c = (pick < 0.85 ? COOL_A.clone().lerp(COOL_B, rand()) : pick < 0.95 ? WARM.clone() : BLUE.clone()).multiplyScalar(0.75);
		col[i * 3] = c.r;
		col[i * 3 + 1] = c.g;
		col[i * 3 + 2] = c.b;
	}
	const geo = new BufferGeometry();
	geo.setAttribute('position', new BufferAttribute(pos, 3));
	geo.setAttribute('color', new BufferAttribute(col, 3));
	const mat = new PointsMaterial({
		size: 2.4,
		sizeAttenuation: true,
		vertexColors: true,
		transparent: true,
		opacity: 0.6,
		depthWrite: false,
	});
	const points = new Points(geo, mat);
	points.renderOrder = -1;
	points.frustumCulled = false;
	return points;
}

export function buildStarfield(shellRadius: number, scale = 1): { group: Group; twinkler: Twinkler } {
	const group = new Group();
	const rand = mulberry(0x517cc1);
	let twinkler: Twinkler | null = null;
	for (const base of CLASSES) {
		const cls = { count: Math.max(Math.round(base.count * scale), 50), size: base.size };
		const pos = new Float32Array(cls.count * 3);
		const col = new Float32Array(cls.count * 3);
		for (let i = 0; i < cls.count; i++) {
			const theta = 2 * Math.PI * rand();
			const phi = Math.acos(2 * rand() - 1);
			const r = shellRadius * (0.95 + 0.1 * rand());
			pos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
			pos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
			pos[i * 3 + 2] = r * Math.cos(phi);

			const pick = rand();
			const c = pick < 0.85 ? COOL_A.clone().lerp(COOL_B, rand()) : pick < 0.95 ? WARM.clone() : BLUE.clone();
			// About 3% of the large class is lifted to HDR brightness and gets bloom to itself —— the only few "real stars"
			if (cls.size >= 3.0 && rand() < 0.03) c.multiplyScalar(1.8);
			col[i * 3] = c.r;
			col[i * 3 + 1] = c.g;
			col[i * 3 + 2] = c.b;
		}
		const geo = new BufferGeometry();
		geo.setAttribute('position', new BufferAttribute(pos, 3));
		geo.setAttribute('color', new BufferAttribute(col, 3));
		const mat = new PointsMaterial({
			size: cls.size,
			sizeAttenuation: false,
			vertexColors: true,
			transparent: true,
			opacity: 0.55,
			depthWrite: false,
		});
		const points = new Points(geo, mat);
		points.renderOrder = -1; // the starfield sits at the bottom
		group.add(points);
		if (cls.size >= 3.0) twinkler = new Twinkler(geo, cls.count); // only the "real star" class twinkles
	}
	return { group, twinkler: twinkler ?? new Twinkler(new BufferGeometry().setAttribute('color', new BufferAttribute(new Float32Array(3), 3)), 1) };
}
