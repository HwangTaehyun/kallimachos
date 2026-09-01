import { Vector3 } from 'three';
import type { FlyPathOptions } from '../interactions/CameraDirector';
import { safeFrameSeconds } from '../timing/frameClock';

/** The verbs GraphController injects —— TourDirector never touches WebGL, it only calls these */
export interface TourHooks {
	nodeCount: () => number;
	degreeOf: (i: number) => number;
	nodePosition: (i: number, out: Vector3) => Vector3;
	graphRadius: () => number;
	selectNode: (i: number, fly: boolean) => void;
	clearSelection: () => void;
	recenter: () => void;
	flyPath: (waypoints: Vector3[], durMs: number, opts: FlyPathOptions) => void;
	beginIdleOrbit: () => void;
	onPath: () => boolean;
	/** A run-state change (keeping the panel button's play/stop in sync) */
	onStateChange?: (running: boolean) => void;
}

type TourKind = 'wander' | 'guided';

/** Every few beats, insert a flyby to give the wander some cinematic variation (the other beats are "fly to a node → orbit → pop the card") */
const WANDER_FLYBY_EVERY = 4;

/**
 * The tour state machine (the v0.3 direction-C rework).  tick(deltaS) is driven by GraphController's rAF (inside the paused guard),
 * so a hidden view freezes naturally and resumes seamlessly.  Two intents:
 * - **wander**: a single automatic director merging "review / flyby / grand tour" into one —— mostly flying to weighted nodes one by one
 *   (degree × long-unseen, so hubs get visited naturally), orbiting and popping the card, with a spline flyby inserted every WANDER_FLYBY_EVERY beats for variation.
 * - **guided (connect two notes)**: walk the precomputed shortest path node by node (called by GraphController once it has both points and the BFS).
 */
export class TourDirector {
	private running = false;
	private kind: TourKind = 'wander';
	private speed = 1;
	private dwellRemainingMs = 0;
	private beat = 0;
	private visited = new Set<number>();
	private queue: number[] = []; // guided = the shortest path
	private queueIdx = 0;
	private tmp = new Vector3();

	constructor(private hooks: TourHooks) {}

	get isRunning(): boolean {
		return this.running;
	}

	/** Wander: one-click ambient automatic touring (no mode to choose) */
	startWander(speed: number): void {
		if (this.hooks.nodeCount() === 0) return;
		this.kind = 'wander';
		this.speed = Math.max(0.2, speed);
		this.running = true;
		this.visited.clear();
		this.beat = 0;
		this.dwellRemainingMs = 0; // start the first beat on the next tick
		this.hooks.onStateChange?.(true);
	}

	/** Connect two notes: walk the shortest path node by node */
	startGuided(path: number[], speed: number): void {
		if (path.length < 2) return;
		this.kind = 'guided';
		this.speed = Math.max(0.2, speed);
		this.running = true;
		this.queue = path.slice();
		this.queueIdx = 0;
		this.dwellRemainingMs = 0;
		this.hooks.onStateChange?.(true);
	}

	/** The user stopped: fall back to the familiar idle drift */
	stop(): void {
		if (!this.running) return;
		this.running = false;
		this.hooks.clearSelection();
		this.hooks.beginIdleOrbit();
		this.hooks.onStateChange?.(false);
	}

	/** A silent abort for cases like a data rebuild: it does not touch the camera (the caller will rebuild and clear the selection) */
	abort(): void {
		if (!this.running) return;
		this.running = false;
		this.hooks.onStateChange?.(false);
	}

	setSpeed(v: number): void {
		this.speed = Math.max(0.2, v);
	}

	tick(deltaS: number): void {
		if (!this.running) return;
		const frameMs = safeFrameSeconds(deltaS) * 1000;
		if (this.kind === 'guided') {
			this.tickGuided(frameMs);
			return;
		}
		this.tickWander(frameMs);
	}

	// ---------- wander ----------

	private tickWander(frameMs: number): void {
		if (this.hooks.onPath()) return; // a flyby leg is in progress
		this.dwellRemainingMs = Math.max(this.dwellRemainingMs - frameMs, 0);
		if (this.dwellRemainingMs > 0) return; // currently orbiting the present node
		this.beat++;
		if (this.beat % WANDER_FLYBY_EVERY === 0) {
			this.startFlybyLeg(); // a cinematic flyby leg; onPath gates until it ends
			return;
		}
		const next = this.pickRediscover();
		if (next < 0) return;
		this.hooks.selectNode(next, true);
		this.dwellRemainingMs = Math.max(2600, 5000 / this.speed);
	}

	// ---------- connect two notes ----------

	private tickGuided(frameMs: number): void {
		this.dwellRemainingMs = Math.max(this.dwellRemainingMs - frameMs, 0);
		if (this.dwellRemainingMs > 0) return;
		const next = this.queueIdx < this.queue.length ? (this.queue[this.queueIdx++] ?? -1) : -1;
		if (next < 0) {
			this.finish(); // the path is walked → return to the overview and wrap up
			return;
		}
		this.hooks.selectNode(next, true);
		this.dwellRemainingMs = Math.max(2600, 5000 / this.speed);
	}

	// ---------- picking ----------

	private pickRediscover(): number {
		const n = this.hooks.nodeCount();
		if (n === 0) return -1;
		if (this.visited.size >= n) this.visited.clear(); // everything has been seen → start again
		let best = -1;
		let bestScore = -1;
		const tries = Math.min(64, n);
		for (let k = 0; k < tries; k++) {
			const i = Math.floor(Math.random() * n);
			if (this.visited.has(i)) continue;
			const score = (this.hooks.degreeOf(i) + 1) * (0.5 + Math.random()); // degree-weighted → hubs get visited more often
			if (score > bestScore) {
				bestScore = score;
				best = i;
			}
		}
		if (best < 0) for (let i = 0; i < n && best < 0; i++) if (!this.visited.has(i)) best = i;
		if (best >= 0) this.visited.add(best);
		return best;
	}

	// ---------- flyby ----------

	private startFlybyLeg(): void {
		const R = this.hooks.graphRadius();
		const wp: Vector3[] = [this.randomShell(R * 2.2)];
		for (let k = 0; k < 3; k++) {
			const i = this.pickRediscover();
			if (i < 0) continue;
			this.hooks.nodePosition(i, this.tmp);
			wp.push(this.tmp.clone().add(this.randomVec(R * 0.15)));
		}
		wp.push(this.randomShell(R * 2.2));
		if (wp.length < 2) return; // no path could be assembled, so skip this beat (the next tick takes a review beat)
		this.hooks.flyPath(wp, 7000 / this.speed, { lookMode: 'tangent', lookAhead: R * 0.3, bank: 0.6 });
	}

	/** A finite tour (connect two notes) ending naturally → back to the overview + tell the panel */
	private finish(): void {
		this.running = false;
		this.hooks.recenter();
		this.hooks.onStateChange?.(false);
	}

	private randomShell(r: number): Vector3 {
		const u = Math.random() * 2 - 1;
		const theta = Math.random() * Math.PI * 2;
		const s = Math.sqrt(1 - u * u);
		return new Vector3(s * Math.cos(theta) * r, u * r * 0.5, s * Math.sin(theta) * r); // slightly flattened, leaning towards the disc plane
	}

	private randomVec(r: number): Vector3 {
		return new Vector3((Math.random() * 2 - 1) * r, (Math.random() * 2 - 1) * r, (Math.random() * 2 - 1) * r);
	}
}
