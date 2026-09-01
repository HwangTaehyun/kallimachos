/** The extensions that take part in the Galaxy View graph as ordinary file nodes. */
const GRAPH_EXTENSIONS = new Set(['md', 'canvas']);

export interface FileWithExtension {
	extension: string;
}

/**
 * Picks the graphable files out of Vault.getFiles()'s result.
 * Extensions are compared case-insensitively, for compatibility with vault filenames that keep their original case.
 */
export function selectGraphFiles<T extends FileWithExtension>(files: readonly T[]): T[] {
	return files.filter((file) => GRAPH_EXTENSIONS.has(file.extension.toLowerCase()));
}

/** Reading tags is meaningful only for Markdown files. */
export function isMarkdownFile(file: FileWithExtension): boolean {
	return file.extension.toLowerCase() === 'md';
}
