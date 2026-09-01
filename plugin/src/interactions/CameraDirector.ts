import { CatmullRomCurve3, MOUSE, PerspectiveCamera, Spherical, Vector3 } from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CRUISE, FLY_TO } from '../constants';
import { progress01, safeFrameSeconds } from '../timing/frameClock';

function easeInOutCubic(t: number): number {
	return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

const WORLD_UP = new Vector3(0, 1, 0);
const BANK_MAX = 0.6; // the flyby's maximum bank (radians, ~34°)
// The framing margin: once the camera's vertical FOV exactly contains the node cloud's sphere radius, multiply by this for surrounding whitespace (>1 = a wider global view)
const FRAMING_MARGIN = 1.5;
const DRAG_THRESHOLD_PX = 4; // the pointer has to move past this to count as a "drag" → only then is orbiting interrupted; a plain click (picking) does not interrupt

interface Tween {
	elapsedMs: number;
	durMs: number;
	fromPos: Vector3;
	toPos: Vector3;
	fromTarget: Vector3;
	toTarget: Vector3;
	onDone?: () => void;
}

/** Spline-path motion (for the tour): the camera walks a CatmullRom curve, looking along the tangent ahead or at a fixed point */
interface PathTween {
	elapsedMs: number;
	durMs: number;
	curve: CatmullRomCurve3;
	lookMode: 'tangent' | 'fixed';
	fixedTarget: Vector3;
	lookAhead: number;
	bank: number;
	onDone?: () => void;
}

export interface FlyPathOptions {
	lookMode?: 'tangent' | 'fixed';
	target?: Vector3;
	lookAhead?: number;
	/** 0 = no bank; >0 banks into a turn during a flyby (0..1) */
	bank?: number;
	onDone?: () => void;
}

export interface CameraHooks {
	/** The F key: fly to the current selection (ignored with nothing selected) */
	onFlyToSelected: () => void;
	/** The R key: back to the overview */
	onResetView: () => void;
}

const FLY_KEYS = new Set(['w', 'a', 's', 'd', 'q', 'e']);

/**
 * The camera director: three layers of interaction freedom (G1 feedback, "move it like an FPS / Google Earth")
 * - The orbit base: left-drag to orbit · right-drag / Ctrl(⌘)+left-drag to pan · wheel to zoom
 * - FPS flight: WASD for forward/back/strafe + Q/E for up/down, the speed adapting to the distance from the target, Shift ×3
 * - Choreography: click/search flight tweens, an idle cruise (two incommensurable periods), F to fly to the selection, R back to the overview
 * Keys take effect only while the canvas has focus (tabindex), so Obsidian's global shortcuts are not stolen.
 */
export class CameraDirector {
	cruiseEnabled = true;
	/** The cruise angular-speed multiplier (the panel's "cruise speed" slider) */
	cruiseSpeed = 1;
	/** A tour is in progress: orbiting is forced even with cruise turned off (so there is visible motion after each leg arrives) */
	tourActive = false;
	/** The initial / back-to-overview framing elevation (degrees): disc presets look down on the arms (~50°), the default is 18° */
	private framingElevDeg = 18;

	private controls: OrbitControls;
	private tween: Tween | null = null;
	private path: PathTween | null = null;
	private pathPos = new Vector3();
	private pathTan = new Vector3();
	private pathTan2 = new Vector3();
	private pathRight = new Vector3();
	private pathUp = new Vector3();
	private idleMs = 0;
	private cruiseAnchor: Spherical | null = null;
	private cruiseT = 0;
	private cruiseDir = 1;
	private pendingDensityDir: Vector3 | null = null;
	private pressed = new Set<string>();
	private shiftHeld = false;
	private tmpOffset = new Vector3();
	private tmpSph = new Spherical();
	private tmpDir = new Vector3();
	private tmpRight = new Vector3();
	private disposeFns: (() => void)[] = [];

	constructor(
		private camera: PerspectiveCamera,
		private dom: HTMLElement,
		private hooks: CameraHooks,
	) {
		this.controls = new OrbitControls(camera, dom);
		this.controls.enableDamping = true;
		this.controls.dampingFactor = 0.08;
		dom.tabIndex = 0; // the canvas can take focus → keyboard flight does not affect Obsidian's other shortcuts
		this.bindPointer();
		this.bindKeys();
	}

	get target(): Vector3 {
		return this.controls.target;
	}

	/** Has the user touched the camera even once.  The basis for the cold start's automatic framing. */
	userMoved = false;

	private markInput(): void {
		this.userMoved = true;
		this.idleMs = 0;
		this.cruiseAnchor = null;
		this.tween = null; // any input interrupts a flight (stopping where it is, without a jump)
		this.path = null; // any input interrupts a tour path too
	}

	/** Is a tour path currently running (TourDirector decides when to send the next leg from this) */
	get onPath(): boolean {
		return this.path !== null;
	}

	/**
	 * Fly along a spline path (the tour primitive).  waypoints ≥2; lookMode='tangent' looks ahead (a flyby),
	 * 'fixed' looks at a fixed point.  Zero per-frame allocation: the curve is built once and each frame only samples into a preallocated Vector3.
	 */
	flyPath(waypoints: Vector3[], durMs: number, opts: FlyPathOptions = {}): void {
		// Clean the control points: drop non-finite points (NaN positions) and adjacent duplicates.  Both make
		// CatmullRom's arc-length mapping produce NaN → getPoint indexes an undefined control point → it throws inside update and freezes the whole render loop.
		const pts: Vector3[] = [];
		for (const w of waypoints) {
			if (!Number.isFinite(w.x) || !Number.isFinite(w.y) || !Number.isFinite(w.z)) continue;
			const last = pts[pts.length - 1];
			if (last && last.distanceToSquared(w) < 1e-4) continue;
			pts.push(w.clone());
		}
		if (pts.length < 2) return;
		// 'uniform' rather than the default 'centripetal': the latter takes the square root of the point spacing and is more brittle when a control point is abnormal.
		const curve = new CatmullRomCurve3(pts, false, 'catmullrom', 0.5);
		this.path = {
			elapsedMs: 0,
			durMs: Math.max(1, durMs),
			curve,
			lookMode: opts.lookMode ?? 'tangent',
			fixedTarget: (opts.target ?? new Vector3()).clone(),
			lookAhead: opts.lookAhead ?? 60,
			bank: opts.bank ?? 0,
		};
		if (opts.onDone) this.path.onDone = opts.onDone;
		this.tween = null;
		this.cruiseAnchor = null;
	}

	/** For an external caller (the render loop's safety net) to clear a running path or tween safely and restore a level horizon; does not fire onDone. */
	cancelMotion(): void {
		this.path = null;
		this.tween = null;
		this.camera.up.set(0, 1, 0);
	}

	private bindPointer(): void {
		// Only a **real drag or zoom** interrupts orbiting —— a plain click (picking) should not stop the cruise (the old version stopped on press and took 10s to resume = far too easy to interrupt).
		let downX = 0;
		let downY = 0;
		let dragging = false;
		const onDown = (e: PointerEvent) => {
			this.dom.focus();
			// Google Earth-style panning: ⌘/Shift/Ctrl + left-drag (macOS reserves Ctrl+click as a right click,
			// so ⌘ or Shift are the ones to use on a Mac; right-drag pans natively)
			this.controls.mouseButtons.LEFT =
				e.metaKey || e.shiftKey || e.ctrlKey ? MOUSE.PAN : MOUSE.ROTATE;
			downX = e.clientX;
			downY = e.clientY;
			dragging = false;
			// Note: markInput is not called here —— it is a drag only once movement passes the threshold
		};
		const onMove = (e: PointerEvent) => {
			if (dragging || e.buttons === 0) return; // not held down is not a drag
			if (Math.hypot(e.clientX - downX, e.clientY - downY) > DRAG_THRESHOLD_PX) {
				dragging = true;
				this.markInput(); // the drag is established → take over the camera and stop orbiting (OrbitControls reads the current camera each frame, so there is no jump)
			}
		};
		const onWheel = () => this.markInput(); // a zoom is an unambiguous action, so it interrupts directly
		const onTouchMove = () => this.markInput(); // touch: only a drag or pinch interrupts (a tap fires no touchmove)
		this.dom.addEventListener('pointerdown', onDown, { capture: true });
		this.dom.addEventListener('pointermove', onMove);
		this.dom.addEventListener('wheel', onWheel, { passive: true });
		this.dom.addEventListener('touchmove', onTouchMove, { passive: true });
		this.disposeFns.push(() => {
			this.dom.removeEventListener('pointerdown', onDown, { capture: true });
			this.dom.removeEventListener('pointermove', onMove);
			this.dom.removeEventListener('wheel', onWheel);
			this.dom.removeEventListener('touchmove', onTouchMove);
		});
	}

	private bindKeys(): void {
		const onKeyDown = (e: KeyboardEvent) => {
			const k = e.key.toLowerCase();
			this.shiftHeld = e.shiftKey;
			if (FLY_KEYS.has(k)) {
				this.pressed.add(k);
				this.markInput();
				e.preventDefault();
				e.stopPropagation();
			} else if (k === 'f') {
				this.hooks.onFlyToSelected();
				e.preventDefault();
			} else if (k === 'r') {
				this.hooks.onResetView();
				e.preventDefault();
			}
		};
		const onKeyUp = (e: KeyboardEvent) => {
			this.shiftHeld = e.shiftKey;
			this.pressed.delete(e.key.toLowerCase());
		};
		const onBlur = () => this.pressed.clear();
		this.dom.addEventListener('keydown', onKeyDown);
		this.dom.addEventListener('keyup', onKeyUp);
		this.dom.addEventListener('blur', onBlur);
		this.disposeFns.push(() => {
			this.dom.removeEventListener('keydown', onKeyDown);
			this.dom.removeEventListener('keyup', onKeyUp);
			this.dom.removeEventListener('blur', onBlur);
		});
	}

	/** WASD/QE: the camera and the orbit target pan together, so orbiting still works after flying */
	private applyFly(deltaS: number): boolean {
		if (this.pressed.size === 0) return false;
		const dist = this.camera.position.distanceTo(this.controls.target);
		const speed = Math.min(Math.max(dist * 0.8, 10), 600) * (this.shiftHeld ? 3 : 1);
		const fwd = this.camera.getWorldDirection(this.tmpDir);
		this.tmpRight.crossVectors(fwd, this.camera.up).normalize();
		const move = new Vector3();
		if (this.pressed.has('w')) move.add(fwd);
		if (this.pressed.has('s')) move.sub(fwd);
		if (this.pressed.has('d')) move.add(this.tmpRight);
		if (this.pressed.has('a')) move.sub(this.tmpRight);
		if (this.pressed.has('e')) move.y += 1;
		if (this.pressed.has('q')) move.y -= 1;
		if (move.lengthSq() < 1e-8) return false;
		move.normalize().multiplyScalar(speed * deltaS);
		this.camera.position.add(move);
		this.controls.target.add(move);
		this.userMoved = true;
		this.idleMs = 0;
		return true;
	}

	/** The initial camera position: a global view around the node cloud's real centre of mass, at its real fitRadius */
	setInitialFraming(center: Vector3, fitRadius: number): void {
		this.camera.position.copy(this.framingPosition(center, fitRadius));
		this.controls.target.copy(center);
		this.controls.update();
	}

	/** Disc presets look down on the arms: a higher elevation (set by applyStylePreset / from the preset at startup) */
	setFramingElev(deg: number): void {
		this.framingElevDeg = deg;
	}

	/**
	 * The framing position: around the centre of mass, at the distance where the camera's vertical FOV exactly contains a sphere of radius fitRadius (× the margin) ——
	 * a true global view, adapting to any vault size or preset spread (replacing the old fixed "seed radius ×3" multiplier staring at the origin).
	 */
	private framingPosition(center: Vector3, fitRadius: number): Vector3 {
		const vfov = (this.camera.fov * Math.PI) / 180;
		const dist = (Math.max(fitRadius, 1) / Math.sin(vfov / 2)) * FRAMING_MARGIN;
		const elev = (this.framingElevDeg * Math.PI) / 180;
		// The view direction: the elevation + a slight z offset for a 3/4 view that reads depth; normalised, multiplied by the distance, then dropped onto the centre of mass
		return new Vector3(Math.cos(elev), Math.sin(elev), 0.35).normalize().multiplyScalar(dist).add(center);
	}

	/** R / recentre: glide back to the overview (around the centre of mass, centred without offset) */
	resetView(center: Vector3, fitRadius: number, onDone?: () => void): void {
		this.startTween(this.framingPosition(center, fitRadius), center.clone(), 1200, onDone);
	}

	/**
	 * Start orbiting the moment a node is reached (without waiting the 10s idle), with the rotation direction preferring the side where the neighbours are dense
	 * (G2 feedback: 5 links, 4 of them south → sweep the south first).
	 */
	beginFocusOrbit(densityDir: Vector3 | null): void {
		this.pendingDensityDir = densityDir;
		this.cruiseAnchor = null;
		this.idleMs = CRUISE.resumeDelayMs + 1;
	}

	flyTo(nodePos: Vector3, nodeRadius: number, onDone?: () => void): void {
		const dist = Math.min(Math.max(nodeRadius * FLY_TO.distancePerRadius, FLY_TO.minDistance), FLY_TO.maxDistance);
		// Keep the current view direction but offset the azimuth by 15° —— it does not face the node head-on on arrival, so the neighbourhood is visible
		const dir = this.camera.position.clone().sub(nodePos);
		if (dir.lengthSq() < 1e-6) dir.set(0, 0, 1);
		this.tmpSph.setFromVector3(dir);
		this.tmpSph.theta += FLY_TO.azimuthOffsetRad;
		this.tmpSph.radius = dist;
		const toPos = nodePos.clone().add(new Vector3().setFromSpherical(this.tmpSph));
		const travel = this.camera.position.distanceTo(toPos);
		const durMs = Math.min(Math.max(FLY_TO.minMs + FLY_TO.msPerWorldUnit * travel, FLY_TO.minMs), FLY_TO.maxMs);
		this.startTween(toPos, nodePos.clone(), durMs, onDone);
	}

	private startTween(toPos: Vector3, toTarget: Vector3, durMs: number, onDone?: () => void): void {
		this.tween = {
			elapsedMs: 0,
			durMs,
			fromPos: this.camera.position.clone(),
			toPos,
			fromTarget: this.controls.target.clone(),
			toTarget,
		};
		if (onDone) this.tween.onDone = onDone;
	}

	/** Driven every frame; returns whether it is currently cruising (for the HUD) */
	update(now: number, animationDeltaS: number, motionDeltaS = animationDeltaS): boolean {
		// Camera motion accumulates frame intervals only and never uses rAF's absolute timestamp.  A popout window
		// can have a different performance.timeOrigin; mixing absolute times produces negative progress and extrapolates the camera into the distance (#14).
		void now;
		const frameS = safeFrameSeconds(animationDeltaS);
		const motionFrameS = safeFrameSeconds(motionDeltaS);
		const frameMs = frameS * 1000;
		if (this.path) {
			const pt = this.path;
			pt.elapsedMs += frameMs;
			const t = progress01(pt.elapsedMs, pt.durMs);
			const u = easeInOutCubic(t);
			// The curve-sampling safety net: even if three.js still hits a boundary exception after the cleaning, it must never bubble up and freeze the whole render loop ——
			// abandon this path in place and hand the horizon back to OrbitControls.
			try {
				pt.curve.getPointAt(u, this.pathPos); // arc-length parameterised → constant speed
			} catch {
				this.camera.up.set(0, 1, 0);
				this.path = null;
				pt.onDone?.();
				this.idleMs = 0;
				return false;
			}
			if (!Number.isFinite(this.pathPos.x) || !Number.isFinite(this.pathPos.y) || !Number.isFinite(this.pathPos.z)) {
				this.camera.up.set(0, 1, 0);
				this.path = null;
				pt.onDone?.();
				this.idleMs = 0;
				return false;
			}
			this.camera.position.copy(this.pathPos);
			if (pt.lookMode === 'tangent') {
				pt.curve.getTangentAt(u, this.pathTan);
				this.controls.target.copy(this.pathPos).addScaledVector(this.pathTan, pt.lookAhead);
				// Banking into a turn: roll up around the view direction by the horizontal turn component of the tangent ahead
				if (pt.bank > 0 && t < 0.999) {
					pt.curve.getTangentAt(Math.min(u + 0.02, 1), this.pathTan2);
					this.pathRight.crossVectors(this.pathTan, WORLD_UP).normalize();
					const turn = this.pathTan2.dot(this.pathRight); // >0 turning right
					const roll = Math.max(Math.min(-turn * pt.bank * 6, BANK_MAX), -BANK_MAX);
					this.pathUp.copy(this.pathTan).normalize(); // the view direction as the roll axis (reusing the scratch)
					this.camera.up.copy(WORLD_UP).applyAxisAngle(this.pathUp, roll).normalize();
				} else {
					this.camera.up.set(0, 1, 0);
				}
			} else {
				this.controls.target.copy(pt.fixedTarget);
				this.camera.up.set(0, 1, 0);
			}
			this.controls.update();
			if (t >= 1) {
				this.camera.up.set(0, 1, 0); // reset the horizon before handing back to OrbitControls
				this.path = null;
				pt.onDone?.();
				this.idleMs = 0; // after arriving, wait out the full idle time before cruising
			}
			return false;
		}

		if (this.tween) {
			const tw = this.tween;
			tw.elapsedMs += frameMs;
			const t = progress01(tw.elapsedMs, tw.durMs);
			const k = easeInOutCubic(t);
			this.camera.position.lerpVectors(tw.fromPos, tw.toPos, k);
			this.controls.target.lerpVectors(tw.fromTarget, tw.toTarget, k);
			this.controls.update();
			if (t >= 1) {
				this.tween = null;
				tw.onDone?.();
				this.idleMs = 0; // after arriving, wait out the full idle time before cruising
			}
			return false;
		}

		const flying = this.applyFly(motionFrameS);

		if (!flying) this.idleMs += frameMs;
		if (!flying && (this.cruiseEnabled || this.tourActive) && this.idleMs > CRUISE.resumeDelayMs) {
			// A smooth ramp from 0 to full speed, with no jolt
			const ramp = Math.min(Math.max((this.idleMs - CRUISE.resumeDelayMs) / CRUISE.rampUpMs, 0), 1);
			if (!this.cruiseAnchor) {
				this.cruiseAnchor = new Spherical().setFromVector3(
					this.tmpOffset.copy(this.camera.position).sub(this.controls.target),
				);
				this.cruiseT = 0;
				this.cruiseDir = 1;
				// The dense-neighbour direction → pick the rotation that sweeps that side sooner
				if (this.pendingDensityDir && this.pendingDensityDir.lengthSq() > 1e-6) {
					const densityTheta = new Spherical().setFromVector3(this.pendingDensityDir).theta;
					let delta = densityTheta - this.cruiseAnchor.theta;
					while (delta > Math.PI) delta -= 2 * Math.PI;
					while (delta < -Math.PI) delta += 2 * Math.PI;
					this.cruiseDir = delta >= 0 ? 1 : -1;
				}
				this.pendingDensityDir = null;
			}
			this.cruiseT += frameS * ramp;
			const t = this.cruiseT;
			const a = this.cruiseAnchor;
			const elev = ((CRUISE.elevationDeg * Math.PI) / 180) * Math.sin((2 * Math.PI * t) / CRUISE.elevationPeriodS);
			const breath = 1 + CRUISE.radiusBreath * Math.sin((2 * Math.PI * t) / CRUISE.radiusPeriodS);
			this.tmpSph.radius = a.radius * breath;
			this.tmpSph.theta = a.theta + this.cruiseDir * CRUISE.angularSpeed * this.cruiseSpeed * t;
			this.tmpSph.phi = Math.min(Math.max(a.phi + elev, 0.05), Math.PI - 0.05);
			this.camera.position.setFromSpherical(this.tmpSph).add(this.controls.target);
			this.camera.lookAt(this.controls.target);
			return true;
		}

		this.controls.update();
		return false;
	}

	dispose(): void {
		this.tween = null;
		this.pressed.clear();
		for (const fn of this.disposeFns) fn();
		this.disposeFns = [];
		this.controls.dispose();
	}
}
