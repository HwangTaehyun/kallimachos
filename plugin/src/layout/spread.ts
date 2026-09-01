/**
 * Is the layout **spread or clumped**, and what should happen then.
 *
 * Why it is a separate file —— this verdict was wrong twice, and both times there was no test.
 * Living only as conditionals inside GraphController, it could not be checked without standing
 * up the whole controller (WebGL, the worker, the Obsidian API), and so nobody checked it.
 */

/** The distance from the origin to the furthest node. */
export function maxRadius(positions: ArrayLike<number>, count: number): number {
	let r2 = 0;
	for (let i = 0; i < count; i++) {
		const x = positions[i * 3] ?? 0, y = positions[i * 3 + 1] ?? 0, z = positions[i * 3 + 2] ?? 0;
		const d = x * x + y * y + z * z;
		if (d > r2) r2 = d;
	}
	return Math.sqrt(r2);
}

/**
 * Are the coordinates clumped together.
 *
 * 1.5× `graphRadius` is the line.  Seeds are scattered at `graphRadius * 4`, so a properly
 * seeded and settled layout is far beyond it.
 *
 * Measured (7,974 entities · graphRadius 256, 2026-08-23):
 *     the initial seed (seedRadius)   160   ← has to be judged clumped
 *     just after seeding             1081
 *     after settling                 1528
 */
export function isCollapsedSpread(
	positions: ArrayLike<number>, count: number, graphRadius: number,
): boolean {
	if (count === 0) return false;      // an empty graph is not "clumped" —— it is absent
	return maxRadius(positions, count) < graphRadius * 1.5;
}

export type ReseedDecision = { reseed: boolean; alpha: number };

/**
 * What to do when the data has changed.
 *
 * ⚠ **Do not look at `warmStart`.**  It is accepted precisely so that "it is accepted and not
 *    used" can be pinned down by a test.  Put this value in a condition and the defect below returns.
 *
 * What happened (measured 2026-08-23)
 *   The web receives the graph over `/api/graph`.  The order goes like this:
 *     ① warm-start from the saved cache (radius 1528) → `warmStart = true`
 *     ② the data arrives and the store **reallocates** the coordinate array, scattering it again
 *        into a seedRadius (=160) ball —— the restored 1528 is thrown away wholesale
 *     ③ the self-healing condition is `!warmStart && collapsed`, so it is **skipped**
 *     ④ `initLayout(0)` —— 0 ticks.  "Settling complete" is declared at radius 160
 *   So a reload was always a clumped ball, and only pressing a preset (re-heating) spread it.
 *   The user asked four times.
 *
 *   `warmStart` is **a state** and clumping is **a result**.  Trust the state and the
 *   reallocation in between is invisible.  Read the result alone and it self-heals whatever the order.
 */
export function reseedDecision(o: {
	collapsed: boolean;
	/** **Not used** in the condition.  See the comment above. */
	warmStart: boolean;
	newcomers: number;
}): ReseedDecision {
	if (o.collapsed) return { reseed: true, alpha: 1 };
	// Heat gently only when new nodes arrived.  Heating after a mere turn-off scatters every
	// remaining star again (user report 2026-08-19).
	return { reseed: false, alpha: o.newcomers > 0 ? 0.3 : 0 };
}

/**
 * Should the camera be re-placed during settling.
 *
 * Why it is needed —— framing happened **once, when settling finished**.  A cold start places
 * the camera at the seed radius (160) and the forces spread the graph to 1,500.
 * Throughout that time (measured 20 seconds) the camera stays **inside** the graph, giving a
 * screen buried among the stars.  That is the other half of "why does it look like this at
 * first" (2026-08-23).
 *
 * Re-placing every frame makes the screen shake.  It re-places **only as much as it grew** ——
 * on a 1.3× criterion, about eight times between 160 and 1,500 (roughly once every 2.5 seconds).
 */
export function shouldReframe(o: {
	radius: number;
	/** The radius when the camera was last placed.  0 if it has not been placed yet. */
	framedRadius: number;
	growth?: number;
}): boolean {
	if (o.radius <= 0) return false;
	if (o.framedRadius <= 0) return true;
	return o.radius > o.framedRadius * (o.growth ?? 1.3);
}
