export interface AnimationFrameOwner {
	requestAnimationFrame(callback: FrameRequestCallback): number;
	cancelAnimationFrame(id: number): void;
}

export type WindowFrameCallback = (now: number, previousNow: number | null) => void;

/**
 * One loop keeps exactly one rAF slot, with an owner.
 *
 * Electron popout windows each own their rAF queue and timestamp time origin; so a window change has to cancel the
 * old request on the original owner, clear the previous frame's time, and immediately start the first frame from the new owner.
 */
export class WindowFrameLoop {
	private owner: AnimationFrameOwner | null = null;
	private frameId: number | null = null;
	private previousNow: number | null = null;
	private generation = 0;
	private disposed = false;

	constructor(private onFrame: WindowFrameCallback) {}

	setOwner(owner: AnimationFrameOwner): void {
		if (this.disposed) return;
		if (this.owner === owner && this.frameId !== null) return;
		this.cancelScheduled();
		this.owner = owner;
		this.previousNow = null;
		this.schedule();
	}

	/** On a visibility pause and resume, discard the elapsed across the paused span and restart timing from 0 on the next frame. */
	resetClock(): void {
		this.previousNow = null;
	}

	private schedule(): void {
		const owner = this.owner;
		if (this.disposed || !owner || this.frameId !== null) return;
		const generation = ++this.generation;
		let frameId = 0;
		frameId = owner.requestAnimationFrame((now) => {
			// After cancelAnimationFrame the browser can still deliver a callback already in the task queue.
			if (
				this.disposed ||
				this.owner !== owner ||
				this.generation !== generation ||
				this.frameId !== frameId
			) return;

			this.frameId = null;
			const previousNow = this.previousNow;
			this.previousNow = now;
			this.onFrame(now, previousNow);

			// onFrame can synchronously trigger a window change or a dispose; neither may be revived on the old owner.
			if (!this.disposed && this.owner === owner && this.generation === generation && this.frameId === null) {
				this.schedule();
			}
		});
		this.frameId = frameId;
	}

	private cancelScheduled(): void {
		this.generation++;
		if (this.owner && this.frameId !== null) this.owner.cancelAnimationFrame(this.frameId);
		this.frameId = null;
	}

	dispose(): void {
		if (this.disposed) return;
		this.disposed = true;
		this.cancelScheduled();
		this.owner = null;
		this.previousNow = null;
	}
}
