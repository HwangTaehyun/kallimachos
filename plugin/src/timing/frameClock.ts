/** rAF's real frame interval: 0 on the first frame and on the first frame after a window change; anomalies and negatives become zero; used for animation timing, and it does not truncate a low frame rate. */
export function elapsedFrameSeconds(now: number, previousNow: number | null): number {
	if (previousNow === null) return 0;
	const elapsedSeconds = (now - previousNow) / 1000;
	if (!Number.isFinite(elapsedSeconds)) return 0;
	return Math.max(elapsedSeconds, 0);
}

/** The simulation and render frame interval: a long-frame cap on top of the safe real interval, avoiding a single-frame jump. */
export function frameDeltaSeconds(now: number, previousNow: number | null, maxSeconds = 0.1): number {
	return Math.min(elapsedFrameSeconds(now, previousNow), maxSeconds);
}

/** Downstream timers accept only a finite, non-negative frame interval. */
export function safeFrameSeconds(deltaS: number): number {
	return Number.isFinite(deltaS) ? Math.max(deltaS, 0) : 0;
}

/** Normalised animation progress; any input lands strictly inside [0, 1]. */
export function progress01(elapsedMs: number, durationMs: number): number {
	if (!Number.isFinite(elapsedMs)) return elapsedMs > 0 ? 1 : 0;
	if (!Number.isFinite(durationMs) || durationMs <= 0) return 1;
	return Math.min(Math.max(elapsedMs / durationMs, 0), 1);
}
