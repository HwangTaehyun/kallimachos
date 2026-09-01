// An esbuild define: true in a dev build, false in a store/release build (the benchmark and other development tools are tree-shaken out)
declare const __GALAXY_DEV__: boolean;

// An esbuild define: true for the standalone browser build (outside Obsidian).  There is no vault,
// so a note-link graph cannot exist — used only as the basis for hiding that UI.
declare const __GALAXY_WEB__: boolean;
