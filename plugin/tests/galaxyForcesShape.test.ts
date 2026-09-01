import { describe, it, expect } from 'vitest';
import { forceRadialCore, forceSpiral } from '../src/layout/galaxyForces';
import type { SimNode } from 'd3-force-3d';

const mk = (xs: number[][]): SimNode[] =>
	xs.map(([x, y, z], i) => ({ index: i, x, y, z, vx: 0, vy: 0, vz: 0 }) as SimNode);

/**
 * `galaxyForces.ts` exports three forces and the tests covered only `forceCommunity`
 * (found by mutation testing on 2026-08-21 —— inverting `forceRadialCore`'s
 * `if (strength === 0) return` to `!==` still passed all 159 tests).
 *
 * And yet these two make the screen's **first impression**.  "Why is it clumped like this at
 * first and never spreads" is exactly a question of these two strengths.  Touched without
 * verification, it can only be judged by eye.
 */
describe('forceRadialCore — a dense core and a radial density gradient', () => {
	const deg = new Int32Array([1, 1]);

	it('pulls nodes towards the central axis', () => {
		const nodes = mk([[100, 0, 0], [0, 0, 100]]);
		const f = forceRadialCore(0.5, deg);
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[0]!.vx!).toBeLessThan(0);   // the +x node moves to -x
		expect(nodes[1]!.vz!).toBeLessThan(0);   // the +z node moves to -z
	});

	it('pulls harder further out (linear in r) —— this is what makes the density gradient', () => {
		const near = mk([[50, 0, 0]]);
		const far = mk([[200, 0, 0]]);
		const a = forceRadialCore(0.5, new Int32Array([1]));
		const b = forceRadialCore(0.5, new Int32Array([1]));
		a.initialize!(near, () => 0, 3); a(1);
		b.initialize!(far, () => 0, 3); b(1);
		expect(Math.abs(far[0]!.vx!)).toBeGreaterThan(Math.abs(near[0]!.vx!));
	});

	it('pulls harder at a higher degree —— a hub sinks into the core', () => {
		const hub = mk([[100, 0, 0]]);
		const leaf = mk([[100, 0, 0]]);
		// The mean degree is computed over each node set, so comparing at the same mean
		// requires putting both nodes in one set.
		const both = mk([[100, 0, 0], [100, 0, 0]]);
		const f = forceRadialCore(0.5, new Int32Array([10, 1]));
		f.initialize!(both, () => 0, 3);
		f(1);
		expect(Math.abs(both[0]!.vx!)).toBeGreaterThan(Math.abs(both[1]!.vx!));
		void hub; void leaf;
	});

	it('does nothing at strength 0', () => {
		const nodes = mk([[100, 0, 0]]);
		const f = forceRadialCore(0, deg);
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[0]!.vx!).toBe(0);
	});

	it('is proportional to alpha —— the force vanishes once settled', () => {
		const run = (alpha: number) => {
			const nodes = mk([[100, 0, 0]]);
			const f = forceRadialCore(0.5, new Int32Array([1]));
			f.initialize!(nodes, () => 0, 3);
			f(alpha);
			return nodes[0]!.vx!;
		};
		expect(run(0.5)).toBeCloseTo(run(1) / 2, 6);
		expect(run(0)).toBe(0);
	});

	it('leaves a node on the central axis alone (it does not divide by zero)', () => {
		const nodes = mk([[0, 50, 0]]);          // r = 0, only y
		const f = forceRadialCore(0.5, new Int32Array([1]));
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[0]!.vx!).toBe(0);
		expect(Number.isFinite(nodes[0]!.vz!)).toBe(true);
	});
});

describe('forceSpiral — spiral arms that are faster inside', () => {
	it('pushes tangentially (not radially)', () => {
		const nodes = mk([[100, 0, 0]]);
		const f = forceSpiral(0.01);
		f.initialize!(nodes, () => 0, 3);
		f(1);
		// A node on the +x axis takes the (-z, x)/r direction → vz positive, vx zero
		expect(nodes[0]!.vz!).toBeGreaterThan(0);
		expect(nodes[0]!.vx!).toBe(0);
	});

	it('is faster inside —— swirl = 1/(1+r/R) attenuates it', () => {
		const spin = (r: number) => {
			const nodes = mk([[r, 0, 0]]);
			const f = forceSpiral(0.01);
			f.initialize!(nodes, () => 0, 3);
			f(1);
			return Math.abs(nodes[0]!.vz!) / r;      // compared as an angular velocity
		};
		expect(spin(40)).toBeGreaterThan(spin(400));
	});

	it('does nothing at strength 0', () => {
		const nodes = mk([[100, 0, 0]]);
		const f = forceSpiral(0);
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[0]!.vz!).toBe(0);
	});

	it('leaves a node on the central axis alone', () => {
		const nodes = mk([[0, 50, 0]]);
		const f = forceSpiral(0.01);
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[0]!.vx!).toBe(0);
		expect(nodes[0]!.vz!).toBe(0);
	});
});
