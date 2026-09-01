import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { maxRadius, isCollapsedSpread, reseedDecision, shouldReframe } from '../src/layout/spread';

const ball = (n: number, r: number) => {
	const p = new Float32Array(n * 3);
	for (let i = 0; i < n; i++) { p[i * 3] = r; p[i * 3 + 1] = 0; p[i * 3 + 2] = 0; }
	return p;
};

describe('maxRadius', () => {
	it('gives the distance to the furthest node', () => {
		const p = new Float32Array([0, 0, 0, 3, 4, 0, 1, 1, 1]);
		expect(maxRadius(p, 3)).toBe(5);
	});
	it('an empty array is 0', () => {
		expect(maxRadius(new Float32Array(0), 0)).toBe(0);
	});
});

describe('isCollapsedSpread', () => {
	//  Uses the measured values as-is (7,974 entities · graphRadius 256)
	it('the initial seed radius (160) is clumped', () => {
		expect(isCollapsedSpread(ball(100, 160), 100, 256)).toBe(true);
	});
	it('just after seeding (1081) is spread', () => {
		expect(isCollapsedSpread(ball(100, 1081), 100, 256)).toBe(false);
	});
	it('after settling (1528) is spread', () => {
		expect(isCollapsedSpread(ball(100, 1528), 100, 256)).toBe(false);
	});
	it('the boundary is 1.5× graphRadius', () => {
		expect(isCollapsedSpread(ball(10, 383), 10, 256)).toBe(true);
		expect(isCollapsedSpread(ball(10, 385), 10, 256)).toBe(false);
	});
	it('an empty graph is not clumped —— it is absent', () => {
		//  Returning true would re-seed and run at alpha 1 whenever a filter switches everything off.
		expect(isCollapsedSpread(new Float32Array(0), 0, 256)).toBe(false);
	});
});

describe('reseedDecision — it must not look at whether it warm-started', () => {
	it('★ it re-seeds when clumped, even after a warm start', () => {
		//  This is what "a reload is always clumped" was.  After warm-starting from the cache,
		//  the data arrives and the store reallocates the coordinates back to the seed radius.
		//  Putting `!warmStart` in the condition means never seeing that moment.
		expect(reseedDecision({ collapsed: true, warmStart: true, newcomers: 0 }))
			.toEqual({ reseed: true, alpha: 1 });
	});

	it('it re-seeds when clumped on a cold start too', () => {
		expect(reseedDecision({ collapsed: true, warmStart: false, newcomers: 0 }))
			.toEqual({ reseed: true, alpha: 1 });
	});

	it('clumping takes priority over new nodes', () => {
		expect(reseedDecision({ collapsed: true, warmStart: true, newcomers: 5 }))
			.toEqual({ reseed: true, alpha: 1 });
	});

	it('spread with new nodes only heats gently', () => {
		expect(reseedDecision({ collapsed: false, warmStart: true, newcomers: 3 }))
			.toEqual({ reseed: false, alpha: 0.3 });
	});

	it('spread with only a turn-off does not touch it at all', () => {
		//  Heating scatters every remaining star again —— the user expects a picture where
		//  "only those stars disappear" (reported 2026-08-19).
		expect(reseedDecision({ collapsed: false, warmStart: false, newcomers: 0 }))
			.toEqual({ reseed: false, alpha: 0 });
	});

	it('warmStart has **no effect at all** on the result', () => {
		for (const collapsed of [true, false]) {
			for (const newcomers of [0, 4]) {
				expect(reseedDecision({ collapsed, warmStart: true, newcomers }))
					.toEqual(reseedDecision({ collapsed, warmStart: false, newcomers }));
			}
		}
	});
});

describe('the GraphController wiring — does it bypass the decision', () => {
	//  This wiring cannot be stood up in a unit test (WebGL · the worker · the Obsidian API).  Instead
	//  it checks that the source does not take **the shape of the defect** again.  That pattern really came back once.
	const src = readFileSync(join(process.cwd(), 'src/view/GraphController.ts'), 'utf8');

	it('it leaves the re-seed decision to reseedDecision', () => {
		expect(src).toContain('reseedDecision({');
		expect(src).toContain('decision.reseed');
	});

	it('★ it does not block re-seeding with warmStart', () => {
		//  `!this.warmStart && this.isCollapsed()` —— that one line was what "a reload is always
		//  clumped" was.  This checks it is gone from the code, not from a comment.
		const code = src.split('\n').filter((l) => !l.trim().startsWith('//') && !l.trim().startsWith('*')).join('\n');
		expect(code).not.toMatch(/!this\.warmStart\s*&&/);
	});

	it('it leaves the clumping verdict to spread.ts', () => {
		//  A criterion split across two places produces a state where one says collapsed and the other does not.
		expect(src).toContain('isCollapsedSpread(');
	});
});

describe('shouldReframe — the camera tracking during settling', () => {
	it('places it when it has never been placed', () => {
		expect(shouldReframe({ radius: 160, framedRadius: 0 })).toBe(true);
	});
	it('does not place it with no graph', () => {
		expect(shouldReframe({ radius: 0, framedRadius: 0 })).toBe(false);
	});
	it('does not place it for a small amount of growth (avoiding shake)', () => {
		//  Re-placing every frame makes the screen shake.
		expect(shouldReframe({ radius: 180, framedRadius: 160 })).toBe(false);
		expect(shouldReframe({ radius: 207, framedRadius: 160 })).toBe(false);   // 1.29×
	});
	it('places it once it grows past 1.3×', () => {
		expect(shouldReframe({ radius: 209, framedRadius: 160 })).toBe(true);    // 1.31×
	});
	it('does not place it when it shrinks', () => {
		expect(shouldReframe({ radius: 800, framedRadius: 1500 })).toBe(false);
	});
	it('it triggers under ten times between 160 and 1500', () => {
		//  Too often and it shakes; too rarely and the buried screen lasts a long time.
		let framed = 0, calls = 0;
		for (let r = 160; r <= 1500; r = r * 1.02) {
			if (shouldReframe({ radius: r, framedRadius: framed })) { framed = r; calls++; }
		}
		expect(calls).toBeGreaterThanOrEqual(6);
		expect(calls).toBeLessThanOrEqual(10);
	});
});
