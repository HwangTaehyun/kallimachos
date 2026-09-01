import type { GraphData, LayoutParams } from '../types';

/**
 * The layout-engine interface —— the isolation layer against the predecessor's wall of death.
 * M1: main-thread d3-force-3d (budgeted: one tick per frame, so the layout process is the animation)
 * M3: a Web Worker implementation (the same interface, positions returned transferable)
 */
export interface LayoutEngine {
	/** x,y,z × n; the engine writes in place and the renderer reads directly */
	readonly positions: Float32Array;
	/** The tick count accumulated since init (for the benchmark, replacing the runtime method-replacement hack) */
	readonly ticks: number;
	/** initialAlpha: 1 = a full cold layout; a low value (0.06 for a warm start, 0.3 for an incremental update) = a gentle tidy-up */
	init(data: GraphData, positions: Float32Array, params: LayoutParams, initialAlpha?: number): void;
	/** Run one tick; false means it has settled */
	step(): boolean;
	isSettled(): boolean;
	/** A gentle re-heat after an incremental data update */
	reheat(alpha?: number): void;
	/** Live parameter changes (the control panel sliders): update the force parameters and re-heat, and the galaxy rearranges on the spot */
	updateParams(params: LayoutParams): void;
	dispose(): void;
}
