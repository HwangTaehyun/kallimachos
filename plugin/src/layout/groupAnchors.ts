/**
 * The **fixed positions** to place groups at.  Spread evenly over a sphere by the golden angle.
 *
 * Why it is in one place —— the seeds (`seedByGroup`) and the force (`forceGroupAnchor`) have to
 * look at **the same positions**.  Computed separately, the seed sends a group to A while the
 * force pulls it to B, and the two fight.
 *
 * Why fixed anchors rather than the centre of mass
 *   `forceCommunity` pulls towards each group's **current centre of mass**.  That force is good
 *   at tightening a clump that is already separated, but **it cannot separate one** —— with every
 *   centre of mass overlapping in the middle there is no direction to pull in.
 *
 *   Topics (Louvain communities) are defined by links, so the links take their side and they
 *   separate on their own.  **Types do not** —— concept and tool are densely connected to each
 *   other, so links mix them instead.  Clumping by type therefore means deciding the positions
 *   from outside and pulling towards them.
 */

/** The golden angle.  Used to spread points evenly over a sphere. */
const GOLDEN = Math.PI * (3 - Math.sqrt(5));

/** Vertical squash.  A galaxy is flat —— the anchors have to be squashed by the same ratio, or they diverge from the seeds. */
export const ANCHOR_FLATTEN = 0.55;

/**
 * The anchor coordinates for k groups.  A `k * 3`-long [x,y,z, x,y,z, …].
 *
 * With k=1 it is a single origin —— there is nothing to separate.
 */
export function groupAnchors(k: number, radius: number): Float64Array {
	const out = new Float64Array(Math.max(0, k) * 3);
	for (let i = 0; i < k; i++) {
		const y = k === 1 ? 0 : 1 - (i / (k - 1)) * 2;
		const r = Math.sqrt(Math.max(0, 1 - y * y));
		const th = GOLDEN * i;
		out[i * 3] = Math.cos(th) * r * radius;
		out[i * 3 + 1] = y * radius * ANCHOR_FLATTEN;
		out[i * 3 + 2] = Math.sin(th) * r * radius;
	}
	return out;
}
