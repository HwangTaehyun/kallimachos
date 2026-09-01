import { describe, expect, it, vi } from 'vitest';
import { Vector3 } from 'three';
import { TourDirector } from '../src/tour/TourDirector';
import type { TourHooks } from '../src/tour/TourDirector';

function mockHooks(n = 10): TourHooks & { selectNode: ReturnType<typeof vi.fn>; flyPath: ReturnType<typeof vi.fn>; recenter: ReturnType<typeof vi.fn>; onStateChange: ReturnType<typeof vi.fn> } {
	return {
		nodeCount: () => n,
		degreeOf: (i) => i,
		nodePosition: (i, out: Vector3) => out.set(i, 0, 0),
		graphRadius: () => 100,
		selectNode: vi.fn(),
		clearSelection: vi.fn(),
		recenter: vi.fn(),
		flyPath: vi.fn(),
		beginIdleOrbit: vi.fn(),
		onPath: () => false,
		onStateChange: vi.fn(),
	};
}

describe('TourDirector state machine', () => {
	it('wander: the first tick after startWander calls selectNode(fly=true)', () => {
		const h = mockHooks();
		const td = new TourDirector(h);
		td.startWander(1);
		expect(h.onStateChange).toHaveBeenCalledWith(true);
		td.tick(0);
		expect(h.selectNode).toHaveBeenCalledTimes(1);
		expect(h.selectNode.mock.calls[0]?.[1]).toBe(true);
	});

	it('wander: it does not jump again within the dwell, and jumps once the dwell is past', () => {
		const h = mockHooks();
		const td = new TourDirector(h);
		td.startWander(1);
		td.tick(0);
		td.tick(0.1); // within the dwell
		expect(h.selectNode).toHaveBeenCalledTimes(1);
		td.tick(5); // past the dwell
		expect(h.selectNode).toHaveBeenCalledTimes(2);
	});

	it('wander: every 4th beat inserts a flyby (flyPath), the rest being review jumps', () => {
		const h = mockHooks();
		const td = new TourDirector(h);
		td.startWander(1);
		td.tick(0); // beat 1 → review
		td.tick(5); // beat 2 → review
		td.tick(5); // beat 3 → review
		td.tick(5); // beat 4 → flyby
		expect(h.selectNode).toHaveBeenCalledTimes(3);
		expect(h.flyPath).toHaveBeenCalledTimes(1);
	});

	it('connect two notes: startGuided walks the path node by node → back to the overview', () => {
		const h = mockHooks();
		const td = new TourDirector(h);
		td.startGuided([0, 1, 2], 1);
		expect(h.onStateChange).toHaveBeenCalledWith(true);
		td.tick(0);
		td.tick(5);
		td.tick(5);
		expect(h.selectNode).toHaveBeenCalledTimes(3);
		td.tick(5); // the queue is walked
		expect(h.recenter).toHaveBeenCalled();
	});

	it('connect two notes: it does not start with fewer than two points on the path', () => {
		const h = mockHooks();
		const td = new TourDirector(h);
		td.startGuided([0], 1);
		expect(td.isRunning).toBe(false);
		td.tick(0);
		expect(h.selectNode).not.toHaveBeenCalled();
	});

	it('tick does nothing before start', () => {
		const h = mockHooks();
		new TourDirector(h).tick(0);
		expect(h.selectNode).not.toHaveBeenCalled();
	});

	it('a negative cross-window frame interval does not end the dwell early', () => {
		const h = mockHooks();
		const td = new TourDirector(h);
		td.startWander(1);
		td.tick(0);
		td.tick(-1_000_000); // the abnormal frame interval a smaller new-window timeOrigin produces
		expect(h.selectNode).toHaveBeenCalledTimes(1);
		td.tick(5);
		expect(h.selectNode).toHaveBeenCalledTimes(2);
	});
});
