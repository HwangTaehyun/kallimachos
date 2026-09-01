import { describe, expect, it } from 'vitest';
import { CURVE_BOW, fillLinkPositions, fillRevealLinkAttributes, segsFor } from '../src/render/linkCurves';

// Two nodes: the source at (10,0,0) and the target at (0,10,0) —— the midpoint sits some distance from the origin, so the radial outward direction is well defined
const positions = new Float32Array([10, 0, 0, 0, 10, 0]);
const links = [{ source: 0, target: 1 }];

describe('segsFor', () => {
	it('curvature 0 is always 1 segment (equivalent to the old straight-line rendering)', () => {
		expect(segsFor(0, 8)).toBe(1);
		expect(segsFor(0.0005, 8)).toBe(1);
	});
	it('with curvature on it takes the tier segment count', () => {
		expect(segsFor(0.5, 8)).toBe(8);
		expect(segsFor(1, 4)).toBe(4);
	});
});

describe('fillLinkPositions', () => {
	it('K=1 straight: the two vertices are the endpoints', () => {
		const out = new Float32Array(6);
		fillLinkPositions(out, positions, links, 1, 0);
		expect([...out]).toEqual([10, 0, 0, 0, 10, 0]);
	});

	it('the first and last curve vertices land exactly on the endpoints (the arc does not detach from the nodes)', () => {
		const K = 8;
		const out = new Float32Array(K * 6);
		fillLinkPositions(out, positions, links, K, 0.6);
		expect(out[0]).toBeCloseTo(10);
		expect(out[1]).toBeCloseTo(0);
		expect(out[2]).toBeCloseTo(0);
		expect(out[(K - 1) * 6 + 3]).toBeCloseTo(0);
		expect(out[(K - 1) * 6 + 4]).toBeCloseTo(10);
		expect(out[(K - 1) * 6 + 5]).toBeCloseTo(0);
	});

	it('the polyline is continuous: each segment starts where the last ended', () => {
		const K = 8;
		const out = new Float32Array(K * 6);
		fillLinkPositions(out, positions, links, K, 0.6);
		for (let i = 1; i < K; i++) {
			expect(out[i * 6]).toBeCloseTo(out[(i - 1) * 6 + 3] ?? NaN);
			expect(out[i * 6 + 1]).toBeCloseTo(out[(i - 1) * 6 + 4] ?? NaN);
			expect(out[i * 6 + 2]).toBeCloseTo(out[(i - 1) * 6 + 5] ?? NaN);
		}
	});

	it('the arc bows radially outwards, with a bow height = curvature × CURVE_BOW × edge length ÷ 2 (the Bézier midpoint property)', () => {
		const K = 8;
		const curvature = 1;
		const out = new Float32Array(K * 6);
		fillLinkPositions(out, positions, links, K, curvature);
		// An even K → segment K/2 starts exactly at the Bézier midpoint at t=0.5
		const mx = out[(K / 2) * 6] ?? 0;
		const my = out[(K / 2) * 6 + 1] ?? 0;
		const mz = out[(K / 2) * 6 + 2] ?? 0;
		const dist = Math.hypot(mx - 5, my - 5, mz - 0); // the chord midpoint (5,5,0)
		const edgeLen = Math.hypot(10 - 0, 0 - 10, 0);
		expect(dist).toBeCloseTo((curvature * CURVE_BOW * edgeLen) / 2, 5);
		// The bow direction = away from the origin
		expect(Math.hypot(mx, my, mz)).toBeGreaterThan(Math.hypot(5, 5, 0));
	});

	it('an undefined edge is left as zeros and skipped, without shifting the edges after it', () => {
		const twoLinks = [undefined, { source: 0, target: 1 }];
		const out = new Float32Array(2 * 6).fill(99);
		fillLinkPositions(out, positions, twoLinks, 1, 0);
		expect([...out.slice(0, 6)]).toEqual([99, 99, 99, 99, 99, 99]); // skipped = not written
		expect([...out.slice(6)]).toEqual([10, 0, 0, 0, 10, 0]);
	});
});

describe('fillRevealLinkAttributes', () => {
	it('uploads the original endpoints once and assigns continuous segment t values', () => {
		const K = 2;
		const source = new Float32Array(K * 2 * 3);
		const target = new Float32Array(K * 2 * 3);
		const curveT = new Float32Array(K * 2);
		fillRevealLinkAttributes(source, target, curveT, positions, links, K);

		expect([...source]).toEqual([10, 0, 0, 10, 0, 0, 10, 0, 0, 10, 0, 0]);
		expect([...target]).toEqual([0, 10, 0, 0, 10, 0, 0, 10, 0, 0, 10, 0]);
		expect([...curveT]).toEqual([0, 0.5, 0.5, 1]);
	});

	it('clears skipped links when persistent reveal attributes are reused', () => {
		const source = new Float32Array(6).fill(99);
		const target = new Float32Array(6).fill(99);
		const curveT = new Float32Array(2).fill(99);
		fillRevealLinkAttributes(source, target, curveT, positions, [undefined], 1);
		expect([...source]).toEqual([0, 0, 0, 0, 0, 0]);
		expect([...target]).toEqual([0, 0, 0, 0, 0, 0]);
		expect([...curveT]).toEqual([0, 0]);
	});
});
