// The esbuild inline-worker plugin: an import with the 'worker:' prefix is bundled into an IIFE text string
declare module 'worker:*' {
	const source: string;
	export default source;
}
