// FNV-1a deterministic hash: shared by the seed layout (so a benchmark is reproducible) and the palette assignment
export function hash32(s: string): number {
	let h = 0x811c9dc5;
	for (let i = 0; i < s.length; i++) {
		h ^= s.charCodeAt(i);
		h = Math.imul(h, 0x01000193);
	}
	return h >>> 0;
}

export function unit(s: string): number {
	return hash32(s) / 0xffffffff;
}

/** Scatter deterministically by id inside a sphere of the given radius, returning [x,y,z] */
export function seedPosition(id: string, radius: number): [number, number, number] {
	const u = unit(id);
	const v = unit(id + ':v');
	const w = unit(id + ':w');
	const r = radius * Math.cbrt(u);
	const theta = 2 * Math.PI * v;
	const phi = Math.acos(2 * w - 1);
	return [r * Math.sin(phi) * Math.cos(theta), r * Math.sin(phi) * Math.sin(theta), r * Math.cos(phi)];
}

/** A seed-sphere radius matched to the node count */
export function seedRadius(nodeCount: number): number {
	return 80 * Math.cbrt(Math.max(nodeCount, 1) / 1000);
}
