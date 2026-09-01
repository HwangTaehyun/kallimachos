// Curved links (v0.4): a quadratic Bézier whose control point = the edge midpoint pushed out along "away from the galactic core (the origin)".
// The layout's centerPull and coreGravity both point at the origin, so arching radially outwards = the arc bends around the bright core,
// and a longer edge arches higher (the bow height ∝ the edge length) —— the orbital-arc feel of NASA Eyes.
// The normal layout-hot window still gathers on the CPU; the creation animation uploads the endpoints + t once and lets the vertex shader
// rebuild the same curve each frame, avoiding repeated uploads of the whole geometry over 2.6s.

/** At full curvature (the slider at 1) the bow height ≈ 32% of the edge length (a starting point, tuned by eye on a real device) */
export const CURVE_BOW = 0.32;

/** The polyline segments per edge: 1 with curvature off, exactly equivalent to straight-line rendering (zero change to geometry or memory) */
export function segsFor(curvature: number, tierSegs: number): number {
	return curvature > 0.001 ? Math.max(tierSegs, 1) : 1;
}

/**
 * Writes the static attributes for the creation animation's link shader: every polyline vertex carries the original edge's two endpoints and the curve parameter t.
 * Called on the first animation, or when the segment count changes mid-animation; the attributes persist and are reused with the geometry, avoiding repeated
 * allocation on a replay and ensuring geometry.dispose fully releases the corresponding GPU buffers.
 */
export function fillRevealLinkAttributes(
	sourceOut: Float32Array,
	targetOut: Float32Array,
	curveTOut: Float32Array,
	positions: Float32Array,
	links: readonly ({ source: number; target: number } | undefined)[],
	K: number,
): void {
	let vertex = 0;
	for (let li = 0; li < links.length; li++) {
		const link = links[li];
		if (!link) {
			for (let skipped = 0; skipped < K * 2; skipped++) {
				const offset = vertex * 3;
				sourceOut[offset] = 0;
				sourceOut[offset + 1] = 0;
				sourceOut[offset + 2] = 0;
				targetOut[offset] = 0;
				targetOut[offset + 1] = 0;
				targetOut[offset + 2] = 0;
				curveTOut[vertex] = 0;
				vertex++;
			}
			continue;
		}
		const source = link.source * 3;
		const target = link.target * 3;
		const sx = positions[source] ?? 0;
		const sy = positions[source + 1] ?? 0;
		const sz = positions[source + 2] ?? 0;
		const tx = positions[target] ?? 0;
		const ty = positions[target + 1] ?? 0;
		const tz = positions[target + 2] ?? 0;
		for (let segment = 0; segment < K; segment++) {
			for (let endpoint = 0; endpoint < 2; endpoint++) {
				const offset = vertex * 3;
				sourceOut[offset] = sx;
				sourceOut[offset + 1] = sy;
				sourceOut[offset + 2] = sz;
				targetOut[offset] = tx;
				targetOut[offset + 1] = ty;
				targetOut[offset + 2] = tz;
				curveTOut[vertex] = (segment + endpoint) / K;
				vertex++;
			}
		}
	}
}

/**
 * Writes the links' polyline vertices into out (the LineSegments layout: K segments per edge = 2K vertices stored contiguously,
 * so out's length must be ≥ links.length·K·6).  O(m·K), called per frame only during the layout-hot window and the creation animation, at zero cost once settled.
 */
export function fillLinkPositions(
	out: Float32Array,
	positions: Float32Array,
	links: readonly ({ source: number; target: number } | undefined)[],
	K: number,
	curvature: number,
): void {
	const bow = curvature * CURVE_BOW;
	let o = 0;
	for (let li = 0; li < links.length; li++) {
		const l = links[li];
		if (!l) {
			o += K * 6;
			continue;
		}
		const s = l.source * 3;
		const t = l.target * 3;
		const sx = positions[s] ?? 0;
		const sy = positions[s + 1] ?? 0;
		const sz = positions[s + 2] ?? 0;
		const tx = positions[t] ?? 0;
		const ty = positions[t + 1] ?? 0;
		const tz = positions[t + 2] ?? 0;
		if (K === 1 || bow <= 0) {
			out[o++] = sx;
			out[o++] = sy;
			out[o++] = sz;
			out[o++] = tx;
			out[o++] = ty;
			out[o++] = tz;
			continue;
		}
		const mx = (sx + tx) / 2;
		const my = (sy + ty) / 2;
		const mz = (sz + tz) / 2;
		const len = Math.hypot(tx - sx, ty - sy, tz - sz);
		const mr = Math.hypot(mx, my, mz);
		let dx: number;
		let dy: number;
		let dz: number;
		if (mr > 1e-3) {
			dx = mx / mr;
			dy = my / mr;
			dz = mz / mr;
		} else {
			// The midpoint is almost at the origin (an edge diametrically through the core): the degenerate direction takes the perpendicular of the edge vector × the Y axis
			const ex = tx - sx;
			const ez = tz - sz;
			const pl = Math.hypot(ez, ex);
			if (pl > 1e-6) {
				dx = ez / pl;
				dy = 0;
				dz = -ex / pl;
			} else {
				dx = 1;
				dy = 0;
				dz = 0;
			}
		}
		const h = bow * len;
		const cx = mx + dx * h;
		const cy = my + dy * h;
		const cz = mz + dz * h;
		let px = sx;
		let py = sy;
		let pz = sz;
		for (let i = 1; i <= K; i++) {
			const tt = i / K;
			const w0 = (1 - tt) * (1 - tt);
			const w1 = 2 * (1 - tt) * tt;
			const w2 = tt * tt;
			const qx = w0 * sx + w1 * cx + w2 * tx;
			const qy = w0 * sy + w1 * cy + w2 * ty;
			const qz = w0 * sz + w1 * cz + w2 * tz;
			out[o++] = px;
			out[o++] = py;
			out[o++] = pz;
			out[o++] = qx;
			out[o++] = qy;
			out[o++] = qz;
			px = qx;
			py = qy;
			pz = qz;
		}
	}
}
