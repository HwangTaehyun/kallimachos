import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * A `//` comment in a JSX **child position** is printed on screen as text.
 *
 * It was caught three times in this session alone, and the last time it really appeared at the top
 * of the settings screen like this:
 *
 *     // `xl:` (1280px) barely applies on this screen —— the window has to be widened a lot for two
 *     columns. // Until then the status and the log stack vertically and push the pipeline off screen.
 *
 * The type checker and the build both pass —— syntactically it is **just text**.
 * Only looking at the screen reveals it.  So it is caught here.
 *
 * A `//` in an expression position (after `{cond && (`) is fine.  The only distinction is
 * **whether the previous line ends with a JSX opening tag**.
 */

function tsxFiles(dir: string): string[] {
	return readdirSync(dir).flatMap((e) => {
		const p = join(dir, e);
		if (statSync(p).isDirectory()) return tsxFiles(p);
		return p.endsWith('.tsx') ? [p] : [];
	});
}

describe('no // comments in a JSX child position', () => {
	it('using one prints it on screen as text', () => {
		const bad: string[] = [];
		//  vitest runs from the package root.  Resolving with `import.meta.url` gives `/src` in some
		//  environments (measured 2026-08-23 —— run that way it died with ENOENT, and the mutation
		//  check looked like it "caught" something.  An already-broken test catches anything).
		const root = join(process.cwd(), 'src');
		for (const f of tsxFiles(root)) {
			const lines = readFileSync(f, 'utf8').split('\n');
			for (let i = 1; i < lines.length; i++) {
				const prev = lines[i - 1]!.trimEnd();
				const cur = lines[i]!.trim();
				if (!cur.startsWith('//')) continue;
				//  When the previous line is a JSX opening tag, this is a children position.
				//  `/>` (self-closing) and `=>` (an arrow function) are excluded.
				if (prev.endsWith('>') && !prev.endsWith('/>') && !prev.endsWith('=>')) {
					bad.push(`${f.slice(root.length + 1)}:${i + 1}  ${cur.slice(0, 60)}`);
				}
			}
		}
		expect(bad, `Wrap a comment in a JSX child position in {/* */}:\n${bad.join('\n')}`).toEqual([]);
	});
});
