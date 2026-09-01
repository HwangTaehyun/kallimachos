import { describe, it, expect } from 'vitest';
import { shouldRefocus, closeMenu } from '../src/web/menuFocus';

/**
 * Deciding whether closing a menu returns focus to the invoking button.
 *
 * `hide()` is called on **any** click in the document (the away listener).  Calling focus()
 * unconditionally steals focus from the unrelated control just pressed and drags it to the menu's
 * invoking button —— r2-verify measured that in a browser through activeElement.
 *
 * There is no DOM environment, so only `contains` is imitated.  That is all the decision needs.
 */
const menu = (inside: unknown[]) => ({ contains: (n: unknown) => inside.includes(n) });

describe('shouldRefocus — a click outside the menu does not steal focus', () => {
	const item = {} as Node;
	const outside = {} as Node;

	it('returns focus when it was inside the menu (an item chosen, or Esc)', () => {
		expect(shouldRefocus(menu([item]), item)).toBe(true);
	});

	it('leaves it alone when it closed from a click outside', () => {
		expect(shouldRefocus(menu([item]), outside)).toBe(false);
	});

	it('does not return focus when there is none (activeElement=null)', () => {
		expect(shouldRefocus(menu([item]), null)).toBe(false);
	});

	it('does not blow up even with no menu element', () => {
		expect(shouldRefocus(null, item)).toBe(false);
		expect(shouldRefocus(undefined, item)).toBe(false);
	});
});

/**
 * ★ This block is the real lesson of this round.
 *
 * With only the decision (`shouldRefocus`) pulled out, the caller got the order wrong: it called
 * `el.remove()` first and then decided, and with `activeElement` already `<body>` it was
 * **never called once.**  All four `shouldRefocus` tests above still passed —— because the bug
 * was on the untestable side.  Pulling out the pure function alone is not enough.
 *
 * So the order was brought inside `closeMenu`, and this checks directly that focus comes back
 * **even after the removal**.
 */
describe('closeMenu — it decides before it destroys', () => {
	const mk = () => {
		const log: string[] = [];
		const item = {} as Node;
		let inside = [item];
		const el = {
			contains: (n: unknown) => inside.includes(n as Node),
			remove: () => { inside = []; log.push('remove'); },   // removing empties the inside
		};
		const invoker = {
			setAttribute: (k: string, v: string) => log.push(`${k}=${v}`),
			focus: () => log.push('focus'),
		};
		return { el, invoker, item, log };
	};

	it('returns focus after removal when it was inside the menu', () => {
		const { el, invoker, item, log } = mk();
		closeMenu(el, invoker, item);
		expect(log).toEqual(['remove', 'aria-expanded=false', 'focus']);
	});

	it('does not steal it when it closed from a click outside', () => {
		const { el, invoker, log } = mk();
		closeMenu(el, invoker, {} as Node);
		expect(log).toEqual(['remove', 'aria-expanded=false']);
	});

	it('still removes even with no invoking button', () => {
		const { el, item, log } = mk();
		closeMenu(el, null, item);
		expect(log).toEqual(['remove']);
	});

	it('always lowers aria-expanded, regardless of focus', () => {
		for (const active of [{} as Node, null]) {
			const { el, invoker, log } = mk();
			closeMenu(el, invoker, active);
			expect(log).toContain('aria-expanded=false');
		}
	});
});
