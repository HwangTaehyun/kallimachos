import { describe, expect, it } from 'vitest';
import { isMarkdownFile, selectGraphFiles } from '../src/data/graphFiles';

describe('selectGraphFiles', () => {
	it('selects Markdown and Canvas only, keeping the input order and the original objects', () => {
		const files = [
			{ path: 'a.md', extension: 'md' },
			{ path: 'board.canvas', extension: 'canvas' },
			{ path: 'image.png', extension: 'png' },
			{ path: 'data.json', extension: 'json' },
		];

		expect(selectGraphFiles(files)).toEqual([files[0], files[1]]);
	});

	it('the extension is case-insensitive, and other attachments stay excluded', () => {
		const files = [
			{ path: 'UPPER.MD', extension: 'MD' },
			{ path: 'Board.CANVAS', extension: 'CANVAS' },
			{ path: 'fake.canvas.json', extension: 'JSON' },
			{ path: 'no-extension', extension: '' },
		];

		expect(selectGraphFiles(files).map((file) => file.path)).toEqual(['UPPER.MD', 'Board.CANVAS']);
	});
});

describe('isMarkdownFile', () => {
	it('Canvas does not enter the Markdown tag-reading path', () => {
		expect(isMarkdownFile({ extension: 'md' })).toBe(true);
		expect(isMarkdownFile({ extension: 'MD' })).toBe(true);
		expect(isMarkdownFile({ extension: 'canvas' })).toBe(false);
	});
});
