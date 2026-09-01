/** The display radius kept inside the existing 6.5× starfield shell; about 5% inner padding for node halos and perspective. */
export const GRAPH_FIT_RADIUS_FACTOR = 6.2;
/** Measured by link-endpoint weight, this share of the body has to stay inside the safe radius. */
export const GRAPH_BODY_WEIGHT_COVERAGE = 0.95;

export interface GraphDisplayTransform {
	center: [number, number, number];
	scale: number;
	sourceRadius: number;
}

/** The bucket count of the weighted quantile histogram; 2,048 buckets ≈ 0.05% radius resolution, enough for a display layer */
const RADIUS_BUCKETS = 2048;

/** Reused histogram buckets, avoiding a per-frame allocation (this function is called every frame while the layout is hot) */
let radiusHistogram: Float64Array | null = null;
function histogram(): Float64Array {
	if (!radiusHistogram) radiusHistogram = new Float64Array(RADIUS_BUCKETS);
	return radiusHistogram;
}

function finite(v: number | undefined): number {
	return v !== undefined && Number.isFinite(v) ? v : 0;
}

/**
 * Transforms the render coordinates only: the link-density centre moves to the origin, and the body is scaled down proportionally when it exceeds the safe radius.
 * The physics simulation and the position cache keep using source, so a display constraint cannot contaminate them backwards.
 */
export function fitGraphPositions(
	source: ArrayLike<number>,
	target: Float32Array,
	nodeCount: number,
	maxRadius: number,
	nodeWeights?: ArrayLike<number>,
): GraphDisplayTransform {
	const count = Math.min(
		Math.max(Math.floor(nodeCount), 0),
		Math.floor(source.length / 3),
		Math.floor(target.length / 3),
	);
	if (count === 0) return { center: [0, 0, 0], scale: 1, sourceRadius: 0 };

	let totalWeight = 0;
	let cx = 0;
	let cy = 0;
	let cz = 0;
	for (let i = 0; i < count; i++) {
		const weight = Math.max(finite(nodeWeights?.[i]), 0);
		totalWeight += weight;
		cx += finite(source[i * 3]) * weight;
		cy += finite(source[i * 3 + 1]) * weight;
		cz += finite(source[i * 3 + 2]) * weight;
	}
	const useWeights = totalWeight > 0;
	if (useWeights) {
		cx /= totalWeight;
		cy /= totalWeight;
		cz /= totalWeight;
	} else {
		for (let i = 0; i < count; i++) {
			cx += finite(source[i * 3]);
			cy += finite(source[i * 3 + 1]);
			cz += finite(source[i * 3 + 2]);
		}
		cx /= count;
		cy /= count;
		cz /= count;
	}

	let sourceRadius = 0;
	if (useWeights) {
		// The weighted 95th-percentile radius.  The original allocated a fresh [radius, weight] tuple array every frame and sorted all of it (O(N log N) + N allocations),
		// and this function runs every frame while the layout is hot (GraphController's rAF → updatePositions) —— measured at 1.53ms/frame on 9.8k nodes,
		// 9.2% of the 60fps budget, plus the GC pressure of tens of thousands of allocations per frame.  Replaced with a fixed-bucket histogram: O(N), zero allocation.
		// The quantile lands on a bucket's upper bound, an error of ≤ maxWeighted/BUCKETS, biased conservatively (the body sits slightly further in), with no visible loss.
		let maxWeighted = 0;
		for (let i = 0; i < count; i++) {
			if (Math.max(finite(nodeWeights?.[i]), 0) <= 0) continue;
			const dx = finite(source[i * 3]) - cx;
			const dy = finite(source[i * 3 + 1]) - cy;
			const dz = finite(source[i * 3 + 2]) - cz;
			const radius = Math.hypot(dx, dy, dz);
			if (radius > maxWeighted) maxWeighted = radius;
		}
		if (maxWeighted > 0) {
			const hist = histogram();
			hist.fill(0);
			for (let i = 0; i < count; i++) {
				const weight = Math.max(finite(nodeWeights?.[i]), 0);
				if (weight <= 0) continue;
				const dx = finite(source[i * 3]) - cx;
				const dy = finite(source[i * 3 + 1]) - cy;
				const dz = finite(source[i * 3 + 2]) - cz;
				const radius = Math.hypot(dx, dy, dz);
				const b = Math.min(Math.floor((radius / maxWeighted) * RADIUS_BUCKETS), RADIUS_BUCKETS - 1);
				hist[b] = (hist[b] ?? 0) + weight;
			}
			const targetWeight = totalWeight * GRAPH_BODY_WEIGHT_COVERAGE;
			let seenWeight = 0;
			for (let b = 0; b < RADIUS_BUCKETS; b++) {
				seenWeight += hist[b] ?? 0;
				if (seenWeight >= targetWeight) {
					sourceRadius = ((b + 1) / RADIUS_BUCKETS) * maxWeighted;
					break;
				}
			}
			if (sourceRadius === 0) sourceRadius = maxWeighted;
		}
	} else {
		for (let i = 0; i < count; i++) {
			const dx = finite(source[i * 3]) - cx;
			const dy = finite(source[i * 3 + 1]) - cy;
			const dz = finite(source[i * 3 + 2]) - cz;
			sourceRadius = Math.max(sourceRadius, Math.hypot(dx, dy, dz));
		}
	}
	const scale = sourceRadius > maxRadius && maxRadius > 0 ? maxRadius / sourceRadius : 1;

	for (let i = 0; i < count; i++) {
		target[i * 3] = (finite(source[i * 3]) - cx) * scale;
		target[i * 3 + 1] = (finite(source[i * 3 + 1]) - cy) * scale;
		target[i * 3 + 2] = (finite(source[i * 3 + 2]) - cz) * scale;
	}

	return { center: [cx, cy, cz], scale, sourceRadius };
}
