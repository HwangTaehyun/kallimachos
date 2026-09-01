import workerSource from 'worker:./forceWorker.ts';
import type { GraphData, LayoutParams } from '../types';
import { groupIndices } from './groupIndex';
import type { LayoutEngine } from './LayoutEngine';

interface TickMsg {
	type: 'tick';
	buffer: ArrayBuffer;
	alpha: number;
	settled: boolean;
	ticks: number;
}

/**
 * The Worker layout (the M3 performance hardening): d3-force-3d runs inside a Blob URL Worker,
 * with the coordinates returned through a transferable double-buffered ping-pong —— the main thread is left with one 38KB memcpy per frame.
 * On a creation failure (a rare environment) the caller falls back to MainThreadForceLayout.
 */
export class WorkerForceLayout implements LayoutEngine {
	positions: Float32Array = new Float32Array(0);

	private worker: Worker | null = null;
	private url = '';
	private dirty = false;
	private settled = true;
	private _ticks = 0;
	private _dead = false;

	/**
	 * Did the Worker die **asynchronously**.
	 *
	 * When `new Worker(blobURL)` throws synchronously, GraphController catches it and switches to
	 * the main-thread layout.  But in an environment where the origin is null, such as a page
	 * opened over file://, creation passes and it fails **later, through an error event**.
	 * Then the coordinates never move again with nobody the wiser — on screen it is just 'a frozen graph'.
	 * The frame loop reads this flag and switches.
	 */
	get dead(): boolean {
		return this._dead;
	}

	get ticks(): number {
		return this._ticks;
	}

	init(data: GraphData, positions: Float32Array, params: LayoutParams, initialAlpha = 1): void {
		this.disposeWorker();
		this.positions = positions;
		this._ticks = 0;
		this.dirty = false;

		this.url = URL.createObjectURL(new Blob([workerSource], { type: 'text/javascript' }));
		this.worker = new Worker(this.url);
		this._dead = false;
		this.worker.onerror = () => {
			this._dead = true;
		};
		this.worker.onmessage = (e: MessageEvent) => {
			const m = e.data as TickMsg;
			if (m.type !== 'tick') return;
			const incoming = new Float32Array(m.buffer);
			this.positions.set(incoming.subarray(0, this.positions.length));
			this._ticks = m.ticks;
			this.settled = m.settled;
			this.dirty = true;
			// Return the buffer (the transferable ping-pong)
			this.worker?.postMessage({ type: 'buffer', buffer: m.buffer }, [m.buffer]);
		};

		const n = data.nodes.length;
		const posCopy = new Float32Array(positions); // the worker holds its own copy
		const linkIdx = new Uint32Array(data.links.length * 2);
		data.links.forEach((l, i) => {
			linkIdx[i * 2] = l.source;
			linkIdx[i * 2 + 1] = l.target;
		});
		const degrees = new Float32Array(n);
		data.nodes.forEach((node, i) => (degrees[i] = Math.max(node.degree, 1)));
		const comms = groupIndices(data);
		const bufA = new ArrayBuffer(n * 3 * 4);
		const bufB = new ArrayBuffer(n * 3 * 4);

		this.settled = initialAlpha < 0.001;
		this.worker.postMessage(
			{
				type: 'init',
				count: n,
				positions: posCopy.buffer,
				links: linkIdx.buffer,
				degrees: degrees.buffer,
				comms: comms.buffer,
				params,
				initialAlpha,
				bufA,
				bufB,
			},
			[posCopy.buffer, linkIdx.buffer, degrees.buffer, comms.buffer, bufA, bufB],
		);
	}

	/** Returns "the coordinates were updated this frame" —— the caller refreshes the render buffers on it */
	step(): boolean {
		const had = this.dirty;
		this.dirty = false;
		return had;
	}

	isSettled(): boolean {
		return this.settled;
	}

	reheat(alpha = 0.3): void {
		this.settled = false;
		this.worker?.postMessage({ type: 'reheat', alpha });
	}

	updateParams(params: LayoutParams): void {
		this.settled = false;
		this.worker?.postMessage({ type: 'params', params });
	}

	private disposeWorker(): void {
		if (this.worker) {
			this.worker.onmessage = null;
			this.worker.terminate();
		}
		this.worker = null;
		if (this.url) {
			URL.revokeObjectURL(this.url);
			this.url = '';
		}
	}

	dispose(): void {
		this.disposeWorker();
		this.settled = true;
		this.dirty = false;
	}
}
