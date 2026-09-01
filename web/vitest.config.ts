import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `web/` had **zero** tests for a long time.  So all 6 usability defects the 2026-08-21 review
// found —— one of them a data loss that wiped aliases.yml entirely —— were in `web/src`, and no
// check could catch any of them.  The 173 passing tests were all in `plugin/tests/` and not one
// of them rendered a React component.
//
// vitest is not a new choice —— the sibling package `plugin/` uses the same 3.2.6.
export default defineConfig({
	plugins: [react()],
	test: {
		environment: 'jsdom',
		globals: false,
		include: ['tests/**/*.test.tsx', 'tests/**/*.test.ts'],
	},
});
