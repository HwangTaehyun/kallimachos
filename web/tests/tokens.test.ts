import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Do the colour utilities the source uses point at **tokens that actually exist**.
 *
 * Tailwind 4 generates utilities from `@theme`'s `--color-*`.  Use a name that is not there and
 * the class is **not generated**, with no error —— that spot simply inherits the parent colour.
 *
 * That really happened: `text-ink-600` was used and `--color-ink-600` did not exist, so the
 * pipeline arrows meant to be dark inherited the body colour (ink-200) and came out **white**
 * (2026-08-23).  Looking at the screen gives "that is bright", not "that is a typo".
 */

const SRC = join(process.cwd(), 'src');

function walk(dir: string): string[] {
	return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
		const p = join(dir, e.name);
		return e.isDirectory() ? walk(p) : /\.(tsx?|css)$/.test(e.name) ? [p] : [];
	});
}

describe('the design tokens', () => {
	const css = readFileSync(join(SRC, 'index.css'), 'utf8');
	const defined = new Set([...css.matchAll(/--color-([a-z0-9-]+):/g)].map((m) => m[1]!));

	it('reads the colour tokens out of index.css', () => {
		//  Failing to read them makes every test below run on nothing —— a pass would stop meaning "it is fine".
		expect(defined.size).toBeGreaterThan(5);
		expect(defined.has('ink-900')).toBe(true);
	});

	it('every colour utility the source uses is a defined token', () => {
		const files = walk(SRC).filter((f) => f.endsWith('.tsx') || f.endsWith('.ts'));
		const bad: string[] = [];
		for (const f of files) {
			const text = readFileSync(f, 'utf8');
			//  `text-ink-600`, `bg-ink-900/60`, `ring-accent/50`, `border-ink-800`,
			//  `divide-ink-850`, `stroke-live` … only the name after the prefix is read.
			for (const m of text.matchAll(
				/\b(?:text|bg|ring|border|divide|fill|stroke|from|via|to|shadow|outline|accent|caret|decoration)-((?:ink|live|ok|warn|crit|accent)(?:-\d{2,3})?)(?:\/\d+)?\b/g,
			)) {
				const name = m[1]!;
				if (!defined.has(name)) bad.push(`${f.replace(process.cwd() + '/', '')} — ${m[0]}`);
			}
		}
		expect(bad).toEqual([]);
	});
});
