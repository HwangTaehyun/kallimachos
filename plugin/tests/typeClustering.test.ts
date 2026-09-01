import { describe, expect, it } from 'vitest';
import {
	forceCenter, forceLink, forceManyBody, forceSimulation, forceX, forceY, forceZ, type SimNode,
} from 'd3-force-3d';
import { forceCommunity, forceRadialCore } from '../src/layout/galaxyForces';

/**
 * **Why types are not made to clump** —— and the test that holds that reason.
 *
 * The request was "make by-type clump like by-topic".  It was actually built and attached: a
 * force pulling each type to a fixed position on a sphere.  The clumps did separate.  And the
 * screen went wrong, and measuring showed exactly why.
 *
 * Measured (400 nodes · 8 types · 1,200 random links · a target link distance of 62):
 *
 *     anchor    mean link length    same type    different type
 *
 *     0.02           833               199           908     ← 5× the moment it is on
 *
 *     0.60         1,276               104         1,415     ← 23× the target
 *
 * **Connections between different types stretch to the far side of the screen.**  In a knowledge
 * graph distance is relatedness —— overwrite that meaning with type and, since most links cross
 * types, the layout starts telling **a lie** about "what is near what".
 *
 * Tuning cannot avoid it: it is already 5× at the weakest 0.02.  It is a force qualitatively
 * unsuited to this data.
 *
 * Topics (Louvain communities) are the opposite —— a community is **defined by links**, so
 * gathering by community shortens links rather than stretching them.  Which is why by-topic gets cohesion.
 *
 * Type is carried **by colour, not position**.  Colour is where a categorical attribute belongs
 * and position is where structure belongs.  To see one alone, the legend's hover and 'only' already do it.
 *
 * What this test holds: whatever force is added, **the links must not stray far from the target
 * distance.**  Attach a group attraction again and it is caught here.
 */

type N = SimNode & { index?: number; x?: number; y?: number; z?: number };

const N_NODES = 400;
const N_TYPES = 8;
const RADIUS = 300;
const LINK_DISTANCE = 62;

function rng(seed: number): () => number {
	let s = seed >>> 0;
	return () => {
		s = (s * 1664525 + 1013904223) >>> 0;
		return s / 4294967296;
	};
}

/** A graph where most links **cross** types.  A real KG is like that. */
function settle(): { nodes: N[]; links: { source: number | N; target: number | N }[]; group: Int32Array } {
	const r0 = rng(7);
	const group = new Int32Array(N_NODES);
	for (let i = 0; i < N_NODES; i++) group[i] = i % N_TYPES;
	const links: { source: number | N; target: number | N }[] = [];
	for (let i = 0; i < N_NODES * 3; i++) {
		const a = Math.floor(r0() * N_NODES);
		let b = Math.floor(r0() * N_NODES);
		if (a === b) b = (b + 1) % N_NODES;
		links.push({ source: a, target: b });
	}
	const r = rng(11);
	const nodes: N[] = Array.from({ length: N_NODES }, (_, i) => ({
		index: i,
		x: (r() - 0.5) * RADIUS, y: (r() - 0.5) * RADIUS, z: (r() - 0.5) * RADIUS,
	}));
	const sim = forceSimulation(nodes, 3)
		.alphaDecay(1 - Math.pow(0.001, 1 / 300))
		.velocityDecay(0.6)
		.force('link', forceLink(links).distance(LINK_DISTANCE).strength(0.4))
		.force('charge', forceManyBody<N>().strength(-260).distanceMax(800))
		.force('center', forceCenter(0, 0, 0))
		.force('x', forceX<N>(0).strength(0.03))
		.force('y', forceY<N>(0).strength(0.03))
		.force('z', forceZ<N>(0).strength(0.03))
		.force('core', forceRadialCore(-0.08, new Int32Array(N_NODES).fill(3)))
		.force('community', forceCommunity(0.15, group))
		.stop();
	sim.alpha(1);
	for (let i = 0; i < 400; i++) sim.tick();
	return { nodes, links, group };
}

/** d3's forceLink replaces source/target with node objects.  They are read back as indices. */
const idx = (v: number | N): number => (typeof v === 'number' ? v : ((v as N).index ?? 0));

describe('the layout does not lie with distance', () => {
	it('links crossing types do not stretch to the far side of the screen', () => {
		const { nodes, links, group } = settle();
		let cross = 0, n = 0;
		for (const l of links) {
			const a = idx(l.source), b = idx(l.target);
			if (group[a] === group[b]) continue;
			const na = nodes[a]!, nb = nodes[b]!;
			cross += Math.hypot((na.x ?? 0) - (nb.x ?? 0), (na.y ?? 0) - (nb.y ?? 0), (na.z ?? 0) - (nb.z ?? 0));
			n++;
		}
		//  Measured 182 (about 3× the target of 62 —— normal, as it balances against repulsion and gravity).
		//  Attaching type anchors jumps it to 908–1,415.  Preventing that state is what this test is for.
		expect(cross / n).toBeLessThan(400);
	});

	it('the link lengths within a type and across types do not differ much', () => {
		const { nodes, links, group } = settle();
		let same = 0, nSame = 0, cross = 0, nCross = 0;
		for (const l of links) {
			const a = idx(l.source), b = idx(l.target);
			const na = nodes[a]!, nb = nodes[b]!;
			const d = Math.hypot((na.x ?? 0) - (nb.x ?? 0), (na.y ?? 0) - (nb.y ?? 0), (na.z ?? 0) - (nb.z ?? 0));
			if (group[a] === group[b]) { same += d; nSame++; } else { cross += d; nCross++; }
		}
		//  If the two diverge it becomes "they are close because they share a type", and the
		//  relatedness a link means is hidden by the type.  Measured 192 vs 182 (a ratio of 0.95).
		//  At anchor 0.6 it was 104 vs 1,415 (a ratio of 13.6).
		const ratio = (cross / nCross) / (same / nSame);
		expect(ratio).toBeLessThan(2);
	});
});
