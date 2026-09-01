import path from 'node:path';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	// `obsidian` ships types only (no main) —— a module importing it cannot even be resolved in a
	// test.  tests/obsidian.stub.ts fills that place.
	resolve: { alias: { obsidian: path.resolve('./tests/obsidian.stub.ts') } },
	test: {
		// vitest's default exclude is `**/node_modules/**`.  This repository keeps its dependencies
		// in `.node_modules/` with a `node_modules` symlink —— so that Obsidian ignores the folder,
		// which starts with a `.` (the 653 .md files inside it were being picked up as notes).
		// The result is that the default exclude pattern misses it and **the dependencies' own
		// tests**, 32 of them, are collected, so the suite always fails.  Even with the project's
		// 152 tests passing.
		exclude: ['**/node_modules/**', '**/.node_modules/**', '**/dist/**'],
	},
});
