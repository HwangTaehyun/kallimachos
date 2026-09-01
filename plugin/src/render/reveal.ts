/** The first 55% of the creation animation staggers by radius; the last 45% is each node's own expansion window. */
export const REVEAL_DELAY_SPAN = 0.55;
export const REVEAL_ACTIVE_SPAN = 0.45;

function finite(v: number | undefined): number {
	return v !== undefined && Number.isFinite(v) ? v : 0;
}

/** The radial wave scale shared with the node and link vertex shaders. */
export function revealScale(progress: number, radius: number, maxRadius: number): number {
	const p = Number.isFinite(progress) ? Math.min(Math.max(progress, 0), 1) : progress > 0 ? 1 : 0;
	const safeMax = Number.isFinite(maxRadius) && maxRadius > 0 ? maxRadius : 1;
	const safeRadius = Number.isFinite(radius) ? Math.max(radius, 0) : 0;
	const delay = (safeRadius / safeMax) * REVEAL_DELAY_SPAN;
	const local = Math.min(Math.max((p - delay) / REVEAL_ACTIVE_SPAN, 0), 1);
	return 1 - Math.pow(1 - local, 3);
}

export function maxPositionRadius(positions: ArrayLike<number>, nodeCount: number): number {
	let maxRadius = 1;
	const count = Math.min(Math.max(Math.floor(nodeCount), 0), Math.floor(positions.length / 3));
	for (let i = 0; i < count; i++) {
		const radius = Math.hypot(
			finite(positions[i * 3]),
			finite(positions[i * 3 + 1]),
			finite(positions[i * 3 + 2]),
		);
		if (radius > maxRadius) maxRadius = radius;
	}
	return maxRadius;
}

/**
 * Updates only the CPU-side "current display coordinates", for labels, picking and camera queries.
 * The WebGL nodes and links are expanded by a shader on the same progress, with no per-frame geometry upload.
 */
export function fillRevealPositions(
	out: Float32Array,
	target: Float32Array,
	nodeCount: number,
	maxRadius: number,
	progress: number,
): void {
	if (out.buffer === target.buffer) {
		throw new Error('Reveal target and display buffers must not alias');
	}
	const count = Math.min(
		Math.max(Math.floor(nodeCount), 0),
		Math.floor(out.length / 3),
		Math.floor(target.length / 3),
	);
	for (let i = 0; i < count; i++) {
		const x = finite(target[i * 3]);
		const y = finite(target[i * 3 + 1]);
		const z = finite(target[i * 3 + 2]);
		const scale = revealScale(progress, Math.hypot(x, y, z), maxRadius);
		out[i * 3] = x * scale;
		out[i * 3 + 1] = y * scale;
		out[i * 3 + 2] = z * scale;
	}
}
