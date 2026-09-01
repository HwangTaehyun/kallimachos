/**
 * Note filtering (issue #11): decides "what enters the graph", acting on FileRecord[] before buildGraph.
 * A filtered-out note takes the edges pointing at it with it (buildGraph drops an edge it cannot find in indexById), so this layer has zero rendering coupling.
 *
 * Two layers, clearly primary and secondary:
 * 1. **Folder show/hide (primary)** —— the panel's clickable legend lists the top-level folders (a colour dot + a note count) and a click toggles them.
 *    This is 90% of the use: "just show me Projects", "stop Archive smearing the graph".  Zero syntax, discoverable, and it answers
 *    "what do these node colours mean" along the way —— a legend and a filter were always the same thing.
 * 2. **The text query (the fallback escape hatch)** —— left for the **cross-cutting** cases a legend cannot express: naming patterns like
 *    `Index` / `Draft` scattered through every folder (exactly #11's original scenario).  Collapsed by default; it does not take the main slot.
 *
 * The syntax (a subset of core Search):
 *   Index          a bare word → matches the full path (folder names and filename included)
 *   file:Index     matches the filename only (the basename)
 *   path:Daily/    matches the full path only
 *   -file:Index    negation: exclude filenames containing Index
 *   "star wars"    quotes wrap a phrase containing spaces
 *   Several words are an implicit AND
 *
 * Deliberately not done (none has a real use case; when someone asks, then):
 * - body search (content:/line:/block:/section:/task:) —— the core Graph View has it, but that means reading the whole library's body on every keystroke;
 *   on a 3,225-note library that cannot give instant feedback, which breaks the performance discipline.  This filter eats only the path metadata the vault already has, O(N) pure string work.
 * - regex, OR, parenthesised grouping, tag: (tags already have their own switch, and adding it here would tangle with showTags's semantics)
 */

import { topFolder } from './buildGraph';

export type FilterField = 'file' | 'path';

export interface FilterTerm {
	field: FilterField;
	/** Already lowercased; matching is a case-insensitive substring containment */
	value: string;
	negate: boolean;
}

/** An empty array = no filtering (the caller short-circuits on it, at zero cost) */
export type FilterQuery = FilterTerm[];

/** The minimum record shape for filtering; structurally compatible with buildGraph's FileRecord */
export interface FilterableRecord {
	path: string;
	basename: string;
}

const FIELDS: Record<string, FilterField> = { file: 'file', path: 'path' };

/**
 * Splits the query string into terms.  Spaces inside quotes do not split; a `field:` prefix and a leading `-` are parsed within a term.
 * Tolerance first: an invalid form degrades to a bare word rather than erroring —— the filter box is used as it is typed, so intermediate states are necessarily invalid.
 */
export function parseFilterQuery(raw: string): FilterQuery {
	const terms: FilterQuery = [];
	let i = 0;
	const n = raw.length;

	while (i < n) {
		while (i < n && raw[i] === ' ') i++;
		if (i >= n) break;

		let negate = false;
		if (raw[i] === '-') {
			negate = true;
			i++;
		}

		// A field: prefix —— only known fields count; an unknown prefix like `foo:bar` is treated as a bare word whole
		let field: FilterField = 'path';
		const colon = raw.indexOf(':', i);
		if (colon > i) {
			const head = raw.slice(i, colon).toLowerCase();
			const known = FIELDS[head];
			// The colon has to come immediately inside the term (no space between), or it is not a field prefix
			if (known && !head.includes(' ') && !head.includes('"')) {
				field = known;
				i = colon + 1;
			}
		}

		// The value: quoted, read to the closing quote (or to the end if unclosed); otherwise read to a space
		let value = '';
		if (raw[i] === '"') {
			i++;
			const close = raw.indexOf('"', i);
			if (close === -1) {
				value = raw.slice(i);
				i = n;
			} else {
				value = raw.slice(i, close);
				i = close + 1;
			}
		} else {
			const start = i;
			while (i < n && raw[i] !== ' ') i++;
			value = raw.slice(start, i);
		}

		// An empty-valued term (a bare `-`, or `file:` with nothing after it) has no meaning and is discarded
		if (value.length > 0) terms.push({ field, value: value.toLowerCase(), negate });
	}

	return terms;
}

export function matchesFilter(rec: FilterableRecord, query: FilterQuery): boolean {
	for (const term of query) {
		const hay = (term.field === 'file' ? rec.basename : rec.path).toLowerCase();
		if (hay.includes(term.value) === term.negate) return false;
	}
	return true;
}

/** The complete filter state, composed of the legend (folder show/hide) and the escape hatch (the text query) */
export interface NoteFilter {
	/** The top-level folders switched off; an empty set = show everything.  A root-level note's key is '' */
	hiddenFolders: ReadonlySet<string>;
	query: FilterQuery;
}

export const EMPTY_FILTER: NoteFilter = { hiddenFolders: new Set(), query: [] };

export function isFilterActive(f: NoteFilter): boolean {
	return f.hiddenFolders.size > 0 || f.query.length > 0;
}

export function passesFilter(rec: FilterableRecord, f: NoteFilter): boolean {
	if (f.hiddenFolders.size > 0 && f.hiddenFolders.has(topFolder(rec.path))) return false;
	return matchesFilter(rec, f.query);
}

/** Returned as-is when unfiltered (without copying the array), avoiding a pointless walk of the whole library */
export function applyFilter<T extends FilterableRecord>(files: T[], f: NoteFilter): T[] {
	if (!isFilterActive(f)) return files;
	return files.filter((rec) => passesFilter(rec, f));
}

/**
 * The legend data: top-level folder → note count, **descending by note count**.
 * It has to be computed over **unfiltered** files, or switching one folder off makes the other chips' numbers jump.
 * That order is also what the colour hues are allocated by (see palette.assignFolderHues) —— the large folders get the non-colliding hues first.
 */
export function folderStats(files: readonly FilterableRecord[]): { folder: string; count: number }[] {
	const by = new Map<string, number>();
	for (const f of files) {
		const top = topFolder(f.path);
		by.set(top, (by.get(top) ?? 0) + 1);
	}
	return [...by.entries()]
		.map(([folder, count]) => ({ folder, count }))
		.sort((a, b) => b.count - a.count || a.folder.localeCompare(b.folder));
}
