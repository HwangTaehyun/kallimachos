/**
 * Composes the document's visibility and the element's viewport visibility into one paused state.
 * Every bind opens a new generation; an IntersectionObserver callback already queued by the old window is ignored.
 */
export class WindowVisibilityBinding {
	private generation = 0;
	private observer: IntersectionObserver | null = null;
	private doc: Document | null = null;
	private onVisibilityChange: (() => void) | null = null;
	private disposed = false;

	constructor(
		private target: Element,
		private onPausedChange: (paused: boolean) => void,
	) {}

	bind(win: Window, doc: Document): void {
		if (this.disposed) return;
		this.generation++;
		this.releaseCurrent();
		const generation = this.generation;
		// It does not inherit the old observer's false; the new document's hidden state still takes effect immediately in sync.
		let visible = true;
		const isCurrent = () => !this.disposed && this.generation === generation;
		const sync = () => {
			if (!isCurrent()) return;
			this.onPausedChange(doc.hidden || !visible);
		};
		const onVisibilityChange = () => sync();
		this.doc = doc;
		this.onVisibilityChange = onVisibilityChange;
		doc.addEventListener('visibilitychange', onVisibilityChange);

		// The DOM typings put the constructor on globalThis; each of Electron's Windows holds its own at runtime.
		const Observer = (win as Window & { IntersectionObserver: typeof IntersectionObserver }).IntersectionObserver;
		const observer = new Observer((entries) => {
			if (!isCurrent() || this.observer !== observer) return;
			visible = entries[0]?.isIntersecting ?? true;
			sync();
		});
		this.observer = observer;
		observer.observe(this.target);
		sync();
	}

	private releaseCurrent(): void {
		this.observer?.disconnect();
		this.observer = null;
		if (this.doc && this.onVisibilityChange) {
			this.doc.removeEventListener('visibilitychange', this.onVisibilityChange);
		}
		this.doc = null;
		this.onVisibilityChange = null;
	}

	dispose(): void {
		if (this.disposed) return;
		this.disposed = true;
		this.generation++;
		this.releaseCurrent();
	}
}
