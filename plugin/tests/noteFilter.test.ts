import { describe, expect, it } from 'vitest';
import { applyFilter, folderStats, matchesFilter, parseFilterQuery } from '../src/data/noteFilter';

const rec = (path: string) => ({ path, basename: path.slice(path.lastIndexOf('/') + 1).replace(/\.md$/, '') });

const VAULT = [
	rec('Daily/2026-07-15.md'),
	rec('Daily/Index.md'),
	rec('Projects/Galaxy View.md'),
	rec('Projects/Index.md'),
	rec('Archive/Old Index Notes.md'),
	rec('star wars notes.md'),
];

const run = (q: string) => applyFilter(VAULT, { hiddenFolders: new Set<string>(), query: parseFilterQuery(q) }).map((f) => f.path);
const runHidden = (folders: string[]) => applyFilter(VAULT, { hiddenFolders: new Set(folders), query: [] }).map((f) => f.path);

describe('parseFilterQuery', () => {
	it('treats a bare word as a path match', () => {
		expect(parseFilterQuery('Index')).toEqual([{ field: 'path', value: 'index', negate: false }]);
	});

	it('parses field prefixes and negation', () => {
		expect(parseFilterQuery('-file:Index')).toEqual([{ field: 'file', value: 'index', negate: true }]);
		expect(parseFilterQuery('path:Daily')).toEqual([{ field: 'path', value: 'daily', negate: false }]);
	});

	it('keeps spaces inside quotes as one term', () => {
		expect(parseFilterQuery('"star wars"')).toEqual([{ field: 'path', value: 'star wars', negate: false }]);
		expect(parseFilterQuery('-file:"Old Index"')).toEqual([{ field: 'file', value: 'old index', negate: true }]);
	});

	it('splits multiple terms on whitespace', () => {
		expect(parseFilterQuery('  file:a   -path:b  ')).toEqual([
			{ field: 'file', value: 'a', negate: false },
			{ field: 'path', value: 'b', negate: true },
		]);
	});

	it('degrades unknown prefixes to a literal bare term rather than erroring', () => {
		// The filter box is used as it is typed, so an intermediate state is necessarily invalid —— it must not throw
		expect(parseFilterQuery('content:foo')).toEqual([{ field: 'path', value: 'content:foo', negate: false }]);
	});

	it('drops terms that carry no value', () => {
		expect(parseFilterQuery('-')).toEqual([]);
		expect(parseFilterQuery('file:')).toEqual([]);
		expect(parseFilterQuery('   ')).toEqual([]);
	});

	it('reads an unclosed quote to the end instead of dropping the term', () => {
		expect(parseFilterQuery('"star wars')).toEqual([{ field: 'path', value: 'star wars', negate: false }]);
	});
});

describe('applyFilter', () => {
	it('returns the array untouched when nothing is filtered', () => {
		expect(applyFilter(VAULT, { hiddenFolders: new Set<string>(), query: [] })).toBe(VAULT); // the same reference = no pointless copy
	});

	it('includes only matches for a positive filename term (issue #11 example)', () => {
		expect(run('file:"Index"')).toEqual(['Daily/Index.md', 'Projects/Index.md', 'Archive/Old Index Notes.md']);
	});

	it('excludes matches for a negative filename term (issue #11 example)', () => {
		expect(run('-file:"Index"')).toEqual(['Daily/2026-07-15.md', 'Projects/Galaxy View.md', 'star wars notes.md']);
	});

	it('matches folder names via a bare term, since path includes them', () => {
		expect(run('Daily')).toEqual(['Daily/2026-07-15.md', 'Daily/Index.md']);
	});

	it('is case-insensitive', () => {
		expect(run('file:index')).toEqual(run('file:INDEX'));
	});

	it('ANDs multiple terms', () => {
		expect(run('Index -path:Daily')).toEqual(['Projects/Index.md', 'Archive/Old Index Notes.md']);
	});

	it('distinguishes file: from path: (folder name must not satisfy file:)', () => {
		expect(run('path:Projects')).toEqual(['Projects/Galaxy View.md', 'Projects/Index.md']);
		expect(run('file:Projects')).toEqual([]);
	});

	it('matches a quoted phrase containing spaces', () => {
		expect(run('"star wars"')).toEqual(['star wars notes.md']);
	});

	it('can exclude everything without throwing', () => {
		expect(run('file:Index -file:Index')).toEqual([]);
	});
});

describe('matchesFilter', () => {
	it('passes any record when the query is empty', () => {
		expect(matchesFilter(rec('anything.md'), [])).toBe(true);
	});
});

describe('folderStats (the legend data)', () => {
	it('descending by note count, root notes going into "" (ties by name, the empty string first)', () => {
		expect(folderStats(VAULT)).toEqual([
			{ folder: 'Daily', count: 2 },
			{ folder: 'Projects', count: 2 },
			{ folder: '', count: 1 },
			{ folder: 'Archive', count: 1 },
		]);
	});

	it('ties are ordered by name —— the legend order has to be stable, or the chips jump on every rebuild', () => {
		const a = folderStats(VAULT).map((f) => f.folder);
		const b = folderStats([...VAULT].reverse()).map((f) => f.folder);
		expect(a).toEqual(b);
	});

	it('Canvas and Markdown use the same folder-statistics semantics', () => {
		const graphFiles = [
			...VAULT,
			{ path: 'Projects/Roadmap.canvas', basename: 'Roadmap' },
			{ path: 'Root.canvas', basename: 'Root' },
		];
		expect(folderStats(graphFiles)).toEqual([
			{ folder: 'Projects', count: 3 },
			{ folder: '', count: 2 },
			{ folder: 'Daily', count: 2 },
			{ folder: 'Archive', count: 1 },
		]);
	});
});

describe('folder show/hide (a legend click)', () => {
	it('switching one folder off → all of its notes disappear', () => {
		expect(runHidden(['Daily'])).toEqual([
			'Projects/Galaxy View.md',
			'Projects/Index.md',
			'Archive/Old Index Notes.md',
			'star wars notes.md',
		]);
	});

	it('"only Projects" = switching every other one off', () => {
		expect(runHidden(['Daily', 'Archive', ''])).toEqual(['Projects/Galaxy View.md', 'Projects/Index.md']);
	});

	it('a root note can be switched off on its own (the key is the empty string and must not be ignored as falsy)', () => {
		expect(runHidden([''])).not.toContain('star wars notes.md');
		expect(runHidden([''])).toHaveLength(5);
	});

	it('the legend and the text box are AND: exclude Index within Projects', () => {
		const out = applyFilter(VAULT, { hiddenFolders: new Set(['Daily', 'Archive', '']), query: parseFilterQuery('-file:Index') });
		expect(out.map((f) => f.path)).toEqual(['Projects/Galaxy View.md']);
	});

	it('Canvas and Markdown use the same folder and text filtering semantics', () => {
		const graphFiles = [
			...VAULT,
			{ path: 'Projects/Roadmap.canvas', basename: 'Roadmap' },
			{ path: 'Archive/Old.canvas', basename: 'Old' },
		];
		const out = applyFilter(graphFiles, {
			hiddenFolders: new Set(['Archive']),
			query: parseFilterQuery('file:road'),
		});
		expect(out.map((file) => file.path)).toEqual(['Projects/Roadmap.canvas']);
	});
});
