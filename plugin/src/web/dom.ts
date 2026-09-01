/**
 * The DOM extensions Obsidian adds to the HTMLElement/Node prototypes.
 *
 * Why they are needed
 *   galaxy-view's UI code (ControlPanel · OverlayManager · SearchModal) uses Obsidian extensions
 *   like `el.createDiv({cls, text})` more than 280 times.  These come not from the 'obsidian'
 *   module but from **the Obsidian app extending the prototypes at runtime**, so shimming the
 *   module alone gives a TypeError for every one of them in a browser.
 *
 *   Running the UI code unchanged means installing the same extensions here.
 *   Not editing a single line of the fork's UI is the goal, so this side adapts.
 *
 * The basis: the DomElementInfo / createEl signatures in Obsidian's API definition obsidian.d.ts.
 *
 * The type declarations live here too.
 *
 * The real obsidian.d.ts declares these extensions as a global augmentation.  Aliasing `obsidian`
 * to the shim makes that declaration vanish wholesale, so it works at runtime while tsc blocks
 * everything with "Property 'createDiv' does not exist".  Only what is actually used is redeclared.
 */
declare global {
	interface Node {
		empty(): void;
		detach(): void;
		appendText(val: string): void;
		setText(val: string | DocumentFragment): void;
	}
	interface HTMLElement {
		createEl<K extends keyof HTMLElementTagNameMap>(
			tag: K,
			o?: string | DomInfo,
			callback?: (el: HTMLElementTagNameMap[K]) => void,
		): HTMLElementTagNameMap[K];
		createDiv(o?: string | DomInfo, callback?: (el: HTMLDivElement) => void): HTMLDivElement;
		createSpan(o?: string | DomInfo, callback?: (el: HTMLSpanElement) => void): HTMLSpanElement;
		createSvg<K extends keyof SVGElementTagNameMap>(
			tag: K,
			o?: string | DomInfo,
			callback?: (el: SVGElementTagNameMap[K]) => void,
		): SVGElementTagNameMap[K];
		addClass(...classes: string[]): void;
		addClasses(classes: string[]): void;
		removeClass(...classes: string[]): void;
		removeClasses(classes: string[]): void;
		toggleClass(classes: string | string[], value: boolean): void;
		hasClass(cls: string): boolean;
		setAttr(qualifiedName: string, value: string | number | boolean | null): void;
		setAttrs(obj: Record<string, string | number | boolean | null>): void;
		setCssProps(props: Record<string, string>): void;
		setCssStyles(styles: Partial<CSSStyleDeclaration>): void;
		isShown(): boolean;
		show(): void;
		hide(): void;
		toggle(show: boolean): void;
	}
	interface Element {
		// Obsidian puts the class helpers on Element too (ControlPanel uses them on querySelector results)
		addClass(...classes: string[]): void;
		removeClass(...classes: string[]): void;
		toggleClass(classes: string | string[], value: boolean): void;
		hasClass(cls: string): boolean;
	}
	interface SVGElement {
		createSvg<K extends keyof SVGElementTagNameMap>(
			tag: K,
			o?: string | DomInfo,
			callback?: (el: SVGElementTagNameMap[K]) => void,
		): SVGElementTagNameMap[K];
	}
	/** An Obsidian global.  It points at 'the active window's document' to support popout windows. */
	const activeDocument: Document;
	interface Document {
		createEl<K extends keyof HTMLElementTagNameMap>(
			tag: K,
			o?: string | DomInfo,
			callback?: (el: HTMLElementTagNameMap[K]) => void,
		): HTMLElementTagNameMap[K];
		createDiv(o?: string | DomInfo, callback?: (el: HTMLDivElement) => void): HTMLDivElement;
		createSpan(o?: string | DomInfo, callback?: (el: HTMLSpanElement) => void): HTMLSpanElement;
	}
	interface DocumentFragment {
		createEl<K extends keyof HTMLElementTagNameMap>(
			tag: K,
			o?: string | DomInfo,
			callback?: (el: HTMLElementTagNameMap[K]) => void,
		): HTMLElementTagNameMap[K];
		createDiv(o?: string | DomInfo, callback?: (el: HTMLDivElement) => void): HTMLDivElement;
		createSpan(o?: string | DomInfo, callback?: (el: HTMLSpanElement) => void): HTMLSpanElement;
	}
}

interface DomInfo {
	cls?: string | string[];
	text?: string | DocumentFragment;
	attr?: Record<string, string | number | boolean | null>;
	title?: string;
	parent?: Node | null;
	value?: string;
	type?: string;
	prepend?: boolean;
	placeholder?: string;
	href?: string;
}

type Info = string | DomInfo | undefined;

function normalize(o: Info): DomInfo {
	return typeof o === 'string' ? { cls: o } : (o ?? {});
}

function apply(el: HTMLElement, o: DomInfo): void {
	if (o.cls) el.classList.add(...(Array.isArray(o.cls) ? o.cls : o.cls.split(/\s+/)).filter(Boolean));
	if (o.text !== undefined) {
		if (typeof o.text === 'string') el.textContent = o.text;
		else el.appendChild(o.text);
	}
	if (o.attr) {
		for (const [k, v] of Object.entries(o.attr)) {
			if (v === null || v === false) el.removeAttribute(k);
			else el.setAttribute(k, String(v));
		}
	}
	if (o.title !== undefined) el.setAttribute('title', o.title);
	if (o.value !== undefined) (el as HTMLInputElement).value = o.value;
	if (o.type !== undefined) (el as HTMLInputElement).type = o.type;
	if (o.placeholder !== undefined) (el as HTMLInputElement).placeholder = o.placeholder;
	if (o.href !== undefined) (el as HTMLAnchorElement).href = o.href;
}

function createEl(this: Node, tag: string, o?: Info, cb?: (el: HTMLElement) => void): HTMLElement {
	const info = normalize(o);
	const el = document.createElement(tag);
	apply(el, info);
	// Attached to parent when given, otherwise to the calling node (Obsidian's behaviour)
	const host = info.parent ?? this;
	if (info.prepend) host.insertBefore(el, host.firstChild);
	else host.appendChild(el);
	cb?.(el);
	return el;
}

/** SVG has a different namespace and cannot be made with createElement.  Every preset icon uses this. */
function createSvg(this: Node, tag: string, o?: Info, cb?: (el: SVGElement) => void): SVGElement {
	const info = normalize(o);
	const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
	if (info.cls) el.classList.add(...(Array.isArray(info.cls) ? info.cls : info.cls.split(/\s+/)).filter(Boolean));
	if (typeof info.text === 'string') el.textContent = info.text;
	if (info.attr) {
		for (const [k, v] of Object.entries(info.attr)) {
			if (v === null || v === false) el.removeAttribute(k);
			else el.setAttribute(k, String(v));
		}
	}
	(info.parent ?? this).appendChild(el);
	cb?.(el);
	return el;
}

const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
const elemProto = Element.prototype as unknown as Record<string, unknown>;
const svgProto = SVGElement.prototype as unknown as Record<string, unknown>;
const nodeProto = Node.prototype as unknown as Record<string, unknown>;
const docProto = Document.prototype as unknown as Record<string, unknown>;
const fragProto = DocumentFragment.prototype as unknown as Record<string, unknown>;

function def(target: Record<string, unknown>, name: string, fn: unknown): void {
	if (name in target) return; // do not overwrite when loaded inside the real Obsidian
	Object.defineProperty(target, name, { value: fn, writable: true, configurable: true, enumerable: false });
}

// activeDocument: in a browser build with no popout window it is always the current document
if (!('activeDocument' in globalThis)) {
	Object.defineProperty(globalThis, 'activeDocument', { get: () => document, configurable: true });
}

for (const target of [proto, elemProto, svgProto]) def(target, 'createSvg', createSvg);

// The class helpers go on Element too (they are used directly on querySelector results)
for (const target of [elemProto]) {
	def(target, 'addClass', function (this: Element, ...c: string[]) {
		this.classList.add(...c.filter(Boolean));
	});
	def(target, 'removeClass', function (this: Element, ...c: string[]) {
		this.classList.remove(...c.filter(Boolean));
	});
	def(target, 'toggleClass', function (this: Element, c: string | string[], on: boolean) {
		for (const one of Array.isArray(c) ? c : [c]) this.classList.toggle(one, on);
	});
	def(target, 'hasClass', function (this: Element, c: string) {
		return this.classList.contains(c);
	});
}

// The real Obsidian puts these three on Node (`interface Node` in obsidian.d.ts).
// They used to be on HTMLElement/Document/DocumentFragment separately, which breaks a call on a
// node that is an Element but not an HTMLElement, such as an SVGElement.  Node alone covers all four.
for (const target of [nodeProto]) {
	def(target, 'createEl', createEl);
	def(target, 'createDiv', function (this: Node, o?: Info, cb?: (el: HTMLElement) => void) {
		return createEl.call(this, 'div', o, cb);
	});
	def(target, 'createSpan', function (this: Node, o?: Info, cb?: (el: HTMLElement) => void) {
		return createEl.call(this, 'span', o, cb);
	});
}

def(nodeProto, 'empty', function (this: Node) {
	while (this.firstChild) this.removeChild(this.firstChild);
});
def(nodeProto, 'detach', function (this: Node) {
	this.parentNode?.removeChild(this);
});
def(nodeProto, 'appendText', function (this: Node, v: string) {
	this.appendChild(document.createTextNode(v));
});
def(nodeProto, 'setText', function (this: Node, v: string | DocumentFragment) {
	if (typeof v === 'string') this.textContent = v;
	else {
		(this as unknown as { empty(): void }).empty();
		this.appendChild(v);
	}
});

def(proto, 'addClass', function (this: HTMLElement, ...c: string[]) {
	this.classList.add(...c.filter(Boolean));
});
def(proto, 'addClasses', function (this: HTMLElement, c: string[]) {
	this.classList.add(...c.filter(Boolean));
});
def(proto, 'removeClass', function (this: HTMLElement, ...c: string[]) {
	this.classList.remove(...c.filter(Boolean));
});
def(proto, 'removeClasses', function (this: HTMLElement, c: string[]) {
	this.classList.remove(...c.filter(Boolean));
});
def(proto, 'toggleClass', function (this: HTMLElement, c: string | string[], on: boolean) {
	for (const one of Array.isArray(c) ? c : [c]) this.classList.toggle(one, on);
});
def(proto, 'hasClass', function (this: HTMLElement, c: string) {
	return this.classList.contains(c);
});
def(proto, 'setAttr', function (this: HTMLElement, k: string, v: string | number | boolean | null) {
	if (v === null || v === false) this.removeAttribute(k);
	else this.setAttribute(k, String(v));
});
def(proto, 'setAttrs', function (this: HTMLElement, o: Record<string, string | number | boolean | null>) {
	for (const [k, v] of Object.entries(o)) (this as unknown as { setAttr(k: string, v: unknown): void }).setAttr(k, v);
});
def(proto, 'setCssProps', function (this: HTMLElement, props: Record<string, string>) {
	// Obsidian passes both custom properties (--x) and ordinary ones (opacity) through setProperty.
	// Both are really used ('--gx-bottom-inset' and 'opacity' in OverlayManager)
	for (const [k, v] of Object.entries(props)) this.style.setProperty(k, v);
});
def(proto, 'setCssStyles', function (this: HTMLElement, styles: Record<string, string>) {
	Object.assign(this.style, styles);
});
def(proto, 'isShown', function (this: HTMLElement) {
	return this.isConnected && this.style.display !== 'none' && getComputedStyle(this).display !== 'none';
});
def(proto, 'show', function (this: HTMLElement) {
	this.style.display = '';
});
def(proto, 'hide', function (this: HTMLElement) {
	this.style.display = 'none';
});
def(proto, 'toggle', function (this: HTMLElement, on: boolean) {
	this.style.display = on ? '' : 'none';
});

export {};
