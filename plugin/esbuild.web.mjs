/**
 * Builds galaxy-view into **one self-contained HTML file** that opens outside Obsidian.
 *
 * Why this file exists separately
 *   esbuild.config.mjs builds the Obsidian plugin — it leaves `obsidian` external and emits
 *   CommonJS.  In a browser there is nobody to provide that module, so here an alias points at
 *   src/web/obsidian.ts instead.  The rest of the code (renderer, layout, control panel)
 *   **is the same file on both sides**.
 *
 * The output
 *   ../viewer/galaxy.html   a single file with the JS, CSS and graph JSON all inlined.
 *   It has to open over file://, so it makes no external fetch (CORS blocks it).
 *
 * Usage:
 *   node esbuild.web.mjs [path to kdb-graph.json] [output html path]
 */
import esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HOME = os.homedir();
/*  ⚠ **The default is not the author's path.**  A stranger running this build would point at a
    path that does not exist, fail to find the graph file, and get an empty viewer.  With no error.
    (2026-08-25 deep review —— the same default was in three compose files too) */
/*  ⚠ …and it must look **where `just vault` wrote**, not only at the environment.  Every Python
    entry point resolves through `vault_path.vault()` (environment → ~/.kal/config.json → .env),
    and that repair's own comment says "only `schema_v3` read the config and the other nine did
    not".  The shell scripts and this build were both left out of it, so `just export` step ③
    died telling the user to set a variable the settings screen had already stored —— while step
    ① in the same run had found the vault fine.  Reproduced 2026-09-04.
    Asking the resolver rather than restating its precedence here: a fourth copy of that order,
    in a third language, is exactly how this drifted. */
let VAULT = process.env.KAL_VAULT || process.env.VAULT_DIR;
if (!VAULT) {
	try {
		const py = process.env.KAL_PYTHON || 'python3';
		VAULT = execFileSync(py, [path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'vault_path.py')],
		                     { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
	} catch { /* the resolver could not decide either —— the message below is the right one */ }
}
if (!VAULT) {
	console.error('  ❌ Set KAL_VAULT (or VAULT_DIR) —— the folder your notes are in,');
	console.error('     or run `just vault <path>`, which every other entry point already reads.');
	process.exit(1);
}

/* Given --assets <dir>, it emits **two web-app assets** instead of the self-contained HTML.
   The React app (web/) uses the same bundle but does not inline the graph, receiving it from
   /api/graph instead —— served over http, it has none of file://'s CORS constraints, and 6MB
   does not have to be baked into the build output.  Re-running the export shows up on a reload. */
const argv = process.argv.slice(2);
const ai = argv.indexOf('--assets');
const ASSETS = ai >= 0 ? path.resolve(argv[ai + 1] ?? '../web/public/galaxy') : null;
const rest = ai >= 0 ? argv.slice(0, ai) : argv;
/*  ⚠ **The canonical graph is `KAL_HOME/graph_export/`, not inside the vault.**  It moved there
    so the viewer container needs no vault mount at all (`export_kal_graph.py`, and `api/main.go`
    reads only that path).  This default was left pointing at the old copy under
    `.obsidian/plugins/`, so `just export` step ③ died with ENOENT on a vault that has no
    `.obsidian/` —— which is every openwiki bundle.  Reproduced 2026-09-04. */
const KAL_HOME = process.env.KAL_HOME || path.join(HOME, '.kal');
const GRAPH = rest[0] ?? path.join(KAL_HOME, 'graph_export', 'kal-graph.json');
const OUT = rest[1] ?? path.resolve('../viewer/galaxy.html');

/* A 'worker:' import prefix → bundled whole as IIFE text (for a Blob URL Worker).
   It has to match the plugin build so the layout worker behaves identically. */
const inlineWorker = {
	name: 'inline-worker',
	setup(build) {
		build.onResolve({ filter: /^worker:/ }, (args) => ({
			path: path.resolve(path.dirname(args.importer), args.path.slice('worker:'.length)),
			namespace: 'inline-worker',
		}));
		build.onLoad({ filter: /.*/, namespace: 'inline-worker' }, async (args) => {
			const r = await esbuild.build({
				entryPoints: [args.path],
				bundle: true,
				write: false,
				format: 'iife',
				target: 'es2021',
				minify: true,
			});
			return { contents: r.outputFiles[0].text, loader: 'text' };
		});
	},
};

const result = await esbuild.build({
	entryPoints: ['src/web/main.ts'],
	bundle: true,
	write: false,
	format: 'iife',
	platform: 'browser',
	target: 'es2021',
	minify: true,
	sourcemap: false,
	// This is the point — it swaps the 'obsidian' the fork's code imports for the browser stand-in
	alias: { obsidian: path.resolve('./src/web/obsidian.ts') },
	// The same define as the plugin build.  Without it, constructing ControlPanel dies with a ReferenceError
	define: { __GALAXY_DEV__: 'false', __GALAXY_WEB__: 'true' },
	plugins: [inlineWorker],
	logLevel: 'info',
});

const js = result.outputFiles[0].text;
const css = fs.readFileSync('styles.css', 'utf8');
// Escape '<' inside the JSON — a document containing the string "</script>" would end the tag early
const graphJson = ASSETS ? '{}' : fs.readFileSync(GRAPH, 'utf8').replace(/</g, '\\u003c');
const vaultName = path.basename(VAULT);

/* The CSS variables Obsidian used to provide.  Without them every colour in styles.css dies.
   The values match Obsidian's default dark theme. */
const themeVars = `
:root {
	--background-primary: #1e1e1e;
	--background-secondary: #161616;
	--background-modifier-border: #2f2f2f;
	--text-normal: #dadada;
	--text-muted: #9a9a9a;
	--text-faint: #6b6b6b;
	--text-accent: #e0a04d;
	--font-monospace: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
	--font-interface: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
		"Helvetica Neue", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
	color-scheme: dark;
}
`;

/* On the premise that the galaxy owns the whole page.  True only for the self-contained HTML ——
   in the web app React owns the layout, so including this block would put overflow:hidden on
   the settings screen too. */
const pageCss = `
html, body { margin: 0; height: 100%; background: #000; color: var(--text-normal);
	font-family: var(--font-interface); overflow: hidden; }
#galaxy { position: fixed; inset: 0; }
#boot { position: fixed; inset: 0; display: grid; place-items: center;
	color: var(--text-faint); font-size: 13px; letter-spacing: .04em; }
`;

/* Styles for the UI the shim creates (Notice · Menu · SuggestModal).
   In the plugin these were drawn by Obsidian, so they are not in styles.css. */
const shimCss = `
.gxw-notices { position: fixed; top: 16px; right: 16px; z-index: 300;
	display: flex; flex-direction: column; gap: 8px; }
.gxw-notice { background: var(--background-secondary); color: var(--text-normal);
	border: 1px solid var(--background-modifier-border); border-radius: 8px;
	padding: 9px 13px; font-size: 12.5px; max-width: 320px; cursor: pointer;
	box-shadow: 0 8px 26px rgba(0,0,0,.55); }
.gxw-menu { position: fixed; z-index: 320; min-width: 168px; padding: 5px;
	background: var(--background-secondary); border-radius: 9px;
	border: 1px solid var(--background-modifier-border);
	box-shadow: 0 12px 34px rgba(0,0,0,.6); }
/* Changed from a div to a button — this undoes the UA's default border, background, font and centring */
.gxw-menu-item { display: block; width: 100%; padding: 6px 11px; font: inherit; font-size: 12.5px;
	border: 0; border-radius: 6px; background: transparent; text-align: left; cursor: pointer;
	box-shadow: none; color: var(--text-normal); }
.gxw-menu-item:hover { background: rgba(255,255,255,.07); }
/* The keyboard can reach here now, so where it is has to be visible */
.gxw-menu-item:focus-visible { background: rgba(255,255,255,.07);
	outline: 2px solid var(--text-accent, #7f9bff); outline-offset: -2px; }
.gxw-menu-item.is-disabled { opacity: .38; pointer-events: none; }
.gxw-menu-sep { height: 1px; margin: 4px 6px; background: var(--background-modifier-border); }
/* This is a <dialog> (the Modal shim in obsidian.ts).  It undoes every UA default —
   fit-content sizing, margin:auto, border, padding:1em, background/color:canvas.
   Giving display only to [open] is the point: an unconditional flex would override the UA
   hiding a closed dialog with display:none, and the modal would never go away. */
.gxw-modal-container { position: fixed; inset: 0; z-index: 340;
	width: auto; height: auto; max-width: none; max-height: none;
	margin: 0; border: 0; overflow: visible;
	background: transparent; color: inherit; padding: 12vh 0 0; }
.gxw-modal-container[open] { display: flex; align-items: flex-start; justify-content: center; }
/* The background is drawn by .gxw-modal-bg below (::backdrop cannot receive a click, so closing would not work) */
.gxw-modal-container::backdrop { background: transparent; }
.gxw-modal-bg { position: absolute; inset: 0; background: rgba(0,0,0,.55);
	backdrop-filter: blur(2px); }
.gxw-modal { position: relative; width: min(560px, 92vw);
	background: var(--background-secondary); border-radius: 12px;
	border: 1px solid var(--background-modifier-border);
	box-shadow: 0 22px 60px rgba(0,0,0,.66); overflow: hidden; }
.gxw-suggest-input { width: 100%; box-sizing: border-box; padding: 13px 15px;
	font-size: 14px; color: var(--text-normal); background: transparent;
	border: 0; border-bottom: 1px solid var(--background-modifier-border); outline: none;
	font-family: inherit; }
.gxw-suggest-results { max-height: 52vh; overflow-y: auto; padding: 5px; }
.gxw-suggest-item { padding: 7px 11px; border-radius: 7px; cursor: pointer; font-size: 13px; }
.gxw-suggest-item.is-selected { background: rgba(255,255,255,.08); }
.gxw-suggest-item .gx-search-path { font-size: 11px; color: var(--text-faint); margin-top: 2px; }
/* The disclosure banner —— a console.log disappears when the terminal is closed and does not
   travel with the file.  This file is the only artifact designed to be handed to someone else,
   so the notice has to be inside the file.  (2026-08-18 adversarial review, security lens) */
#gx-disclosure {
  position: fixed; left: 12px; right: 12px; bottom: 12px; z-index: 9999;
  display: flex; gap: 10px; align-items: center; justify-content: center;
  padding: 8px 14px; border-radius: 8px;
  background: rgba(28,22,10,.92); border: 1px solid rgba(224,160,77,.45);
  color: #e8dcc4; font-size: 12px; line-height: 1.5;
  font-family: system-ui, -apple-system, sans-serif;
  backdrop-filter: blur(6px); pointer-events: auto;
}
#gx-disclosure button {
  background: none; border: 0; color: #e0a04d; cursor: pointer;
  font-size: 13px; padding: 2px 4px; line-height: 1;
}
#gx-disclosure button:focus-visible { outline: 2px solid #e0a04d; outline-offset: 2px; }
@media (max-width: 560px) { #gx-disclosure { font-size: 11px; padding: 7px 10px; } }
`;

/* Counts what is inside the graph.  The banner (inside the file) and the build log (in the
   terminal) have to state **the same figures**, so they are counted in one place. */
const inv = (() => {
  try {
    const g = JSON.parse(graphJson.replace(/\\u003c/g, '<'));
    const docs = new Set();
    const byType = {};
    for (const e of g.entities ?? []) {
      for (const d of e.docs ?? []) docs.add(d);
      byType[e.type] = (byType[e.type] ?? 0) + 1;
    }
    return { docs: docs.size, ents: (g.entities ?? []).length,
             person: byType.person ?? 0, org: byType.organization ?? 0 };
  } catch { return { docs: 0, ents: 0, person: 0, org: 0 }; }
})();

/* ── the web-app asset mode ───────────────────────────────────────
   The graph is not included, so there is no sharing warning banner either —— these assets run
   only inside our app and are never handed to anyone as a single file.  That warning is a
   property of the self-contained HTML. */
if (ASSETS) {
	const bundleCss = themeVars + shimCss + css;
	/* The content hash goes into the filename.
	   Emitted under a fixed name (galaxy.js), the browser kept running the old bundle —— the server
	   had new code while the screen had old, with no way to tell (measured 2026-08-19).
	   A changed hash changes the URL, so there is no room for a cache to intervene.  Which file to
	   load is then announced by manifest.json. */
	const hash = (t) => crypto.createHash('sha256').update(t).digest('hex').slice(0, 8);
	const jsName = `galaxy.${hash(js)}.js`;
	const cssName = `galaxy.${hash(bundleCss)}.css`;

	fs.rmSync(ASSETS, { recursive: true, force: true });   // so old hashed files do not pile up
	fs.mkdirSync(ASSETS, { recursive: true });
	fs.writeFileSync(path.join(ASSETS, jsName), js);
	fs.writeFileSync(path.join(ASSETS, cssName), bundleCss);
	fs.writeFileSync(path.join(ASSETS, 'manifest.json'),
		JSON.stringify({ js: jsName, css: cssName }, null, 2) + '\n');

	const kb = (n) => `${Math.round(n / 1024)}KB`;
	console.log(`\n  → ${ASSETS}`);
	console.log(`     ${jsName} ${kb(js.length)} · ${cssName} ${kb(bundleCss.length)}`);
	console.log('     manifest.json points at these names (a content hash — no cache can get in)');
	console.log('     The graph is not inlined — the web app receives it from /api/graph');
	process.exit(0);
}

const html = `<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Galaxy view — the entity graph</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='5' fill='%23e0a04d'/%3E%3Cellipse cx='16' cy='16' rx='14' ry='6' fill='none' stroke='%237ba7ff' stroke-width='1.6' transform='rotate(-24 16 16)'/%3E%3C/svg%3E">
<style>${themeVars}${pageCss}${shimCss}${css}</style>
<div id="boot">GALAXY VIEW · starting…</div>
<div id="galaxy"></div>
<script>window.__KAL_VAULT__=${JSON.stringify(vaultName)};window.__KAL_GRAPH__=${graphJson};</script>
<script>${js}</script>
<!--
  ⚠ This file is self-contained.  Copy or forward it as-is and the following goes with it.
     · ${inv.docs} vault document paths — the filename is the topic
     · ${inv.person} person · ${inv.org} organization entities (real names and organisation names possible)
     · the vault name "${vaultName}" · ${inv.ents} entity descriptions
     · infrastructure and hostnames may remain in the entity descriptions
  Credential masking passed, but **identity and infrastructure information is not what it checks.**
  Generated: ${OUT}
-->
<div id="gx-disclosure" role="note">This file is self-contained — ${inv.docs} vault document paths · ${inv.ents} entities are inside it.  Check before sharing.<button type="button" aria-label="Close" onclick="this.parentNode.remove()">✕</button></div>
`;

fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, html);

/* Says what goes into this file, on every build.
   2026-08-18 adversarial review: this HTML is self-contained and therefore **the one artifact
   designed to leave the machine**, and inside it go the vault's document paths (the filename is
   the topic), the person and organization entities, and internal hostnames.  The masking layer
   looks only at credentials, so all of this passes through.
   Rather than remove it, the build says so every time —— so there is one more thought before handing it over. */
function disclose() {
  console.log('\n  ⚠ This HTML is self-contained and can be shared as-is.  What is inside:');
  console.log(`     ${inv.docs} vault document paths (the filename is the topic)`);
  console.log(`     ${inv.person} person · ${inv.org} organization entities`);
  console.log(`     the vault name "${vaultName}" · ${inv.ents} entity descriptions`);
  console.log('     Credential masking passed, but identity and infrastructure information is not what it checks.');
  console.log('     The same wording is inside the file too (a comment + a banner at the foot of the screen).');
}

const mb = (n) => `${(n / 1024 / 1024).toFixed(1)}MB`;
console.log(`\n  → ${OUT}`);
console.log(`     js ${mb(js.length)} · css ${mb(css.length + shimCss.length)} · graph ${mb(graphJson.length)} · total ${mb(html.length)}`);
console.log(`     vault "${vaultName}" — clicking a document path opens it through obsidian://`);
disclose();
