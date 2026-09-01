import type { Force, SimNode } from 'd3-force-3d';

// A single source: both the Worker (forceWorker.ts) and the main-thread fallback
// (MainThreadForceLayout.ts) import from here, so the two implementations cannot drift.
// Both are custom d3-force-3d-shaped forces (force(alpha) + initialize).

/** The normalising radius (roughly the galactic disc's radius), scaling the strength into a sensible range */
const CORE_R = 200;
const SPIRAL_R = 160;

/**
 * Radial core gravity: pulls nodes towards the central axis along the cylindrical radius r=hypot(x,z),
 * more strongly further out (linear in r), and weighted by degree (hubs sink to the core, the degree
 * weight clamped to [0.3,3]) → a dense bright core + a radial density gradient.
 * Scaled by alpha throughout → it tends to zero once settled and does not blow up a warm graph.
 * There is no .strength() setter: to change the strength, rebuild with sim.force('core', forceRadialCore(v, degrees)).
 */
export function forceRadialCore(strength: number, degrees: ArrayLike<number>): Force<SimNode> {
	let nodes: SimNode[] = [];
	let meanDeg = 1;
	const force: Force<SimNode> = (alpha: number) => {
		if (strength === 0) return;
		for (const n of nodes) {
			const x = n.x ?? 0;
			const z = n.z ?? 0;
			const r = Math.hypot(x, z);
			if (r < 1e-4) continue;
			const degW = Math.min(Math.max((degrees[n.index ?? 0] || 1) / meanDeg, 0.3), 3);
			const pull = (strength * degW * alpha * r) / CORE_R; // linear in r
			n.vx = (n.vx ?? 0) - (x / r) * pull;
			n.vz = (n.vz ?? 0) - (z / r) * pull;
		}
	};
	force.initialize = (ns: SimNode[]) => {
		nodes = ns;
		let sum = 0;
		for (const n of ns) sum += degrees[n.index ?? 0] || 1;
		meanDeg = ns.length ? sum / ns.length : 1;
	};
	return force;
}

/**
 * The tangential spiral-arm force: a faint tangential push with swirl=1/(1+r/R), faster inside and slower outside,
 * which combs the disc into logarithmic arms during cooling.  Scaled by alpha + the swirl decay → it stops it "spinning forever".
 */
export function forceSpiral(strength: number): Force<SimNode> {
	let nodes: SimNode[] = [];
	const force: Force<SimNode> = (alpha: number) => {
		if (strength === 0) return;
		for (const n of nodes) {
			const x = n.x ?? 0;
			const z = n.z ?? 0;
			const r = Math.hypot(x, z);
			if (r < 1e-3) continue;
			const swirl = 1 / (1 + r / SPIRAL_R);
			const k = strength * alpha * swirl;
			// The tangent (-z, x)/r, multiplied by r to cancel → a steady tangential linear velocity
			n.vx = (n.vx ?? 0) + (-z / r) * k * r;
			n.vz = (n.vz ?? 0) + (x / r) * k * r;
		}
	};
	force.initialize = (ns: SimNode[]) => {
		nodes = ns;
	};
	return force;
}

/**
 * Community cohesion — pulls nodes of the same community towards that community's centre of mass.
 *
 * Why it is needed
 *   A community Louvain found is a clump **in the connection structure**.  But a force layout
 *   sees only the links, the repulsion and the core gravity, so that clump ends up scattered on
 *   screen.  The colour matches while the positions are mixed, and "viewing by community" does
 *   not hold up as a picture.  Finding each community's centre of mass every tick and pulling
 *   gently towards it clumps them naturally.
 *
 * Why the centre of mass (rather than assigning fixed coordinates in advance)
 *   Assigned in advance, it fights the link forces — a good share of the 12,228 relations cross
 *   communities, and forcing them apart turns those edges into a tangle across the screen.  The
 *   centre-of-mass approach **tightens slightly** the layout the links have already made, so the
 *   two cooperate.
 *
 * comm[i] < 0 (other) is not pulled — those are unrelated to each other and have no reason to clump.
 */
export function forceCommunity(strength: number, comm: ArrayLike<number>): Force<SimNode> {
	let nodes: SimNode[] = [];
	let nComm = 0;
	let cx = new Float64Array(0);
	let cy = new Float64Array(0);
	let cz = new Float64Array(0);
	let cn = new Int32Array(0);

	const force: Force<SimNode> = (alpha: number) => {
		if (strength === 0 || nComm === 0) return;
		cx.fill(0); cy.fill(0); cz.fill(0); cn.fill(0);
		for (const n of nodes) {
			const c = comm[n.index ?? 0] ?? -1;
			if (c < 0) continue;
			cx[c] = (cx[c] ?? 0) + (n.x ?? 0);
			cy[c] = (cy[c] ?? 0) + (n.y ?? 0);
			cz[c] = (cz[c] ?? 0) + (n.z ?? 0);
			cn[c] = (cn[c] ?? 0) + 1;
		}
		for (let c = 0; c < nComm; c++) {
			const k = cn[c] ?? 0;
			if (k > 0) {
				cx[c] = (cx[c] ?? 0) / k;
				cy[c] = (cy[c] ?? 0) / k;
				cz[c] = (cz[c] ?? 0) / k;
			}
		}
		const kk = strength * alpha;
		for (const n of nodes) {
			const c = comm[n.index ?? 0] ?? -1;
			if (c < 0 || (cn[c] ?? 0) === 0) continue;
			n.vx = (n.vx ?? 0) + ((cx[c] ?? 0) - (n.x ?? 0)) * kk;
			n.vy = (n.vy ?? 0) + ((cy[c] ?? 0) - (n.y ?? 0)) * kk;
			n.vz = (n.vz ?? 0) + ((cz[c] ?? 0) - (n.z ?? 0)) * kk;
		}
	};
	force.initialize = (ns: SimNode[]) => {
		nodes = ns;
		let max = -1;
		for (const n of ns) max = Math.max(max, comm[n.index ?? 0] ?? -1);
		nComm = max + 1;
		cx = new Float64Array(nComm); cy = new Float64Array(nComm);
		cz = new Float64Array(nComm); cn = new Int32Array(nComm);
	};
	return force;
}
