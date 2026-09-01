import { describe, it, expect } from 'vitest';
import { forceCommunity } from '../src/layout/galaxyForces';
import type { SimNode } from 'd3-force-3d';

const mk = (xs: number[][]): SimNode[] =>
	xs.map(([x, y, z], i) => ({ index: i, x, y, z, vx: 0, vy: 0, vz: 0 }) as SimNode);

/**
 * Community cohesion.  Get it wrong and the colours match while the positions are mixed, so
 * "viewing by community" does not hold up as a picture.  The strength is 0.15 by measurement (kNN purity 0.643 → 0.686).
 */
describe('forceCommunity', () => {
	it('pulls nodes of the same community towards each other', () => {
		const nodes = mk([[-10, 0, 0], [10, 0, 0]]);
		const f = forceCommunity(0.5, new Int32Array([0, 0]));
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[0]!.vx!).toBeGreaterThan(0);   // the left node moves right
		expect(nodes[1]!.vx!).toBeLessThan(0);      // the right node moves left
	});

	it('different communities do not pull each other', () => {
		const nodes = mk([[-10, 0, 0], [10, 0, 0]]);
		const f = forceCommunity(0.5, new Int32Array([0, 1]));
		f.initialize!(nodes, () => 0, 3);
		f(1);
		// Each is its own community's centre of mass = itself → no reason to move
		expect(Math.abs(nodes[0]!.vx!)).toBeLessThan(1e-9);
		expect(Math.abs(nodes[1]!.vx!)).toBeLessThan(1e-9);
	});

	it('-1 (other) is left alone', () => {
		const nodes = mk([[-10, 0, 0], [10, 0, 0], [50, 0, 0]]);
		const f = forceCommunity(0.5, new Int32Array([0, 0, -1]));
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[2]!.vx!).toBe(0);
	});

	it('at strength 0 it does nothing (the state with type mode off)', () => {
		const nodes = mk([[-10, 0, 0], [10, 0, 0]]);
		const f = forceCommunity(0, new Int32Array([0, 0]));
		f.initialize!(nodes, () => 0, 3);
		f(1);
		expect(nodes[0]!.vx!).toBe(0);
	});

	it('is proportional to alpha —— the force has to vanish once settled', () => {
		const run = (alpha: number) => {
			const n = mk([[-10, 0, 0], [10, 0, 0]]);
			const f = forceCommunity(0.5, new Int32Array([0, 0]));
			f.initialize!(n, () => 0, 3);
			f(alpha);
			return n[0]!.vx!;
		};
		expect(run(0.5)).toBeCloseTo(run(1) / 2, 6);
		expect(run(0)).toBe(0);
	});
});
