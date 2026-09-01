import { describe, expect, it } from 'vitest';
import { fitGraphPositions } from '../src/render/graphTransform';

describe('fitGraphPositions', () => {
	it('moves an off-center graph to the world origin without changing its scale', () => {
		const source = new Float32Array([90, -40, 20, 110, -40, 20]);
		const target = new Float32Array(source.length);
		const transform = fitGraphPositions(source, target, 2, 100);

		expect(transform.center).toEqual([100, -40, 20]);
		expect(transform.scale).toBe(1);
		expect([...target]).toEqual([-10, 0, 0, 10, 0, 0]);
	});

	it('uniformly scales every node inside the existing shell safety radius', () => {
		const source = new Float32Array([-200, 0, 0, 200, 0, 0, 0, 100, 0]);
		const target = new Float32Array(source.length);
		const transform = fitGraphPositions(source, target, 3, 80);
		const radii = [0, 1, 2].map((i) => Math.hypot(target[i * 3] ?? 0, target[i * 3 + 1] ?? 0, target[i * 3 + 2] ?? 0));

		expect(transform.scale).toBeLessThan(1);
		expect(Math.max(...radii)).toBeCloseTo(80, 4);
	});

	it('fits the linked body without letting an isolated outlier shrink it', () => {
		const source = new Float32Array([-100, 0, 0, 100, 0, 0, 1000, 0, 0]);
		const target = new Float32Array(source.length);
		const transform = fitGraphPositions(source, target, 3, 80, new Float32Array([10, 10, 0]));

		expect(transform.center).toEqual([0, 0, 0]);
		expect(transform.scale).toBeCloseTo(0.8, 4);
		expect(target[0]).toBeCloseTo(-80, 4);
		expect(target[3]).toBeCloseTo(80, 4);
		expect(target[6]).toBeCloseTo(800, 4);
	});

	it('handles an empty graph without producing invalid coordinates', () => {
		const transform = fitGraphPositions([], new Float32Array(0), 0, 80);
		expect(transform).toEqual({ center: [0, 0, 0], scale: 1, sourceRadius: 0 });
	});

	/**
	 * The only case that **actually discriminates** the weighted-quantile branch.
	 *
	 * The existing weighted case put both nodes at **the same radius 100**, so all the weight fell
	 * into a single histogram bucket —— changing coverage from 0.95 to 0.10, or the bucket count
	 * from 2048 to 4, gave the same answer (both r4-guard mutations passed).
	 * The other 3 cases pass no nodeWeights and fall into the max-radius branch.
	 *
	 * This value decides, every frame, "how much of the graph's body to fit inside the 6.2× sphere".
	 * Wrong, and the galaxy is either clipped outside the shell or rendered shrunk to a point ——
	 * with no error, looking like "the physics is odd".
	 *
	 * It is left-right symmetric, so the centre of mass is pinned at the origin and the radii scatter over 5..100.
	 */
	it('uses the weighted 95th-percentile radius (not the maximum)', () => {
		const rs: number[] = [];
		for (let r = 5; r <= 100; r += 5) rs.push(r);
		const n = rs.length * 2;
		const source = new Float32Array(n * 3);
		const weights = new Float32Array(n);
		rs.forEach((r, i) => {
			source[2 * i * 3] = r;
			source[(2 * i + 1) * 3] = -r;
			weights[2 * i] = 1;
			weights[2 * i + 1] = 1;
		});
		const t = fitGraphPositions(source, new Float32Array(n * 3), n, 80, weights);

		expect(t.center).toEqual([0, 0, 0]);
		//  The 95th percentile = 95 (within +0.05 from bucket quantisation).  **Not the maximum of 100.**
		expect(t.sourceRadius).toBeGreaterThan(94);
		expect(t.sourceRadius).toBeLessThan(96);
		//  Lowering coverage falls below this; coarsening the buckets rises above it
		expect(t.sourceRadius).toBeLessThan(99);
	});
});
