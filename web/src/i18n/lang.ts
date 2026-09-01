import { useSyncExternalStore } from 'react';

/**
 * The UI language.  **English is the default** (the user's decision, 2026-09-01) —— 'en' when nothing is saved or it is corrupted.
 *
 * Why localStorage —— the language is a preference of whoever uses this browser, not a property of
 * the server (the vault).  Two people can view the same server in different languages.  Put it in
 * the server settings (config.json) and one person's choice applies to everyone.
 */
export type Lang = 'en' | 'ko';
const KEY = 'kal.lang';
const EVENT = 'kal:lang';

export function getLang(): Lang {
	try {
		return localStorage.getItem(KEY) === 'ko' ? 'ko' : 'en';
	} catch {
		return 'en';
	}
}

export function setLang(l: Lang): void {
	try {
		localStorage.setItem(KEY, l);
	} catch {
		/* Applied for this session even when saving is blocked */
	}
	window.dispatchEvent(new Event(EVENT));
}

const subscribe = (cb: () => void) => {
	window.addEventListener(EVENT, cb);
	window.addEventListener('storage', cb); // when another tab changed it
	return () => {
		window.removeEventListener(EVENT, cb);
		window.removeEventListener('storage', cb);
	};
};

/** Read the language during a render, and redraw when it changes. */
export function useLang(): Lang {
	return useSyncExternalStore(subscribe, getLang, () => 'en');
}

/**
 * The per-component string dictionary.  `{ en: {...}, ko: {...} }` are kept **in the same file** ——
 * a key drifting apart is caught by the types (ko has to follow en's key set).
 * A value is a string, or a function when there is a placeholder: `count: (n: number) => \`${n} items\``.
 * A key missing from ko falls back to en —— a late translation does not give a blank screen.
 */
export type Strings = Record<string, string | ((...args: never[]) => string)>;
/** A ko string value is not tied to en's **literal** —— en written `as const` narrowing to 'Reload' still lets ko take its own wording. */
export type Loose<S extends Strings> = { [K in keyof S]?: S[K] extends string ? string : S[K] };
export type Dict<S extends Strings> = { en: S; ko: Loose<S> };

/** Outside a hook (api.ts, estimate.ts and so on): picks by the language at call time. */
export function tr<S extends Strings>(d: Dict<S>, lang: Lang = getLang()): <K extends keyof S>(k: K) => S[K] {
	return (k) => ((lang === 'ko' ? d.ko[k] : undefined) ?? d.en[k]) as S[typeof k];
}

/** Inside a component: redraws when the language changes. */
export function useT<S extends Strings>(d: Dict<S>): <K extends keyof S>(k: K) => S[K] {
	return tr(d, useLang());
}
