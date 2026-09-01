// Deciding where focus goes when a menu closes.  **There is a reason it is its own module.**
//
// `obsidian.ts` calls `matchMedia(...)` at module top level (for `Platform`).  So importing it
// from node blows up at once, and this repository has no DOM test environment
// (neither jsdom nor happy-dom is installed).  The result was that **not one** test imported
// that file —— it was a file that could not be tested.
//
// Rather than add a dependency or tear open a shared module, only the decision was moved here.
// It is tested with fake objects, no DOM required.
/** Whether closing a menu should return focus to the button that invoked it.
 *
 * It should when: the user was **inside** the menu (they chose an item, or pressed Esc).
 * Otherwise focus drops to `<body>` and the tab order rewinds to the start of the document.
 *
 * It should **not** when: it closed because of a click outside.  `hide()` is called on any click
 * anywhere in the document, so an unconditional focus() steals focus from the unrelated control
 * just pressed and drags it to the menu's invoking button (measured in a browser by r2-verify).
 *
 * This repository has no DOM test environment (neither jsdom nor happy-dom), so the decision
 * alone is pulled out —— that can be tested with fake objects. */
export function shouldRefocus(
	menuEl: { contains(n: Node | null): boolean } | null | undefined,
	active: Node | null,
): boolean {
	return !!menuEl && !!active && menuEl.contains(active);
}

/** Closes the menu —— **the order is the point.**
 *
 * With only `shouldRefocus` pulled out, the caller got the order wrong:
 *
 *     this.el.remove();                                   // ← focus goes to <body> here
 *     if (shouldRefocus(this.el, document.activeElement))  // ← always false
 *
 * With focus inside a removed subtree, `document.activeElement` becomes `<body>` and
 * `el.contains(body)` is false.  So it was **never called once.**  `aria-expanded` turned off
 * correctly, so it looked fine.  (r4-ux round 5, measured 2026-08-21)
 *
 * Pulling out the decision alone was not enough —— the bug stayed on the untestable side (the
 * caller's ordering).  **The order is brought in here.**  Now "does focus come back even after
 * removal" can be tested directly with fake objects.
 */
export function closeMenu(
	el: { contains(n: Node | null): boolean; remove(): void },
	invoker: { setAttribute(k: string, v: string): void; focus(): void } | null,
	active: Node | null,
): void {
	//  ★ decided **before** anything is destroyed
	const wasInside = shouldRefocus(el, active);
	el.remove();
	if (!invoker) return;
	invoker.setAttribute('aria-expanded', 'false');
	if (wasInside) invoker.focus();
}
