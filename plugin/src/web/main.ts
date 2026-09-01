/**
 * The entry point that brings galaxy-view up outside Obsidian (in a browser).
 *
 * What differs and what does not
 *   The same — renderer, layout, control panel, overlay and presets are all **the same code** as
 *          the plugin.  It constructs GraphController directly.  What you see is identical.
 *   Different — it reads a kal-graph.json frozen at build time instead of a vault, and instead of
 *          opening a note it wakes desktop Obsidian through an obsidian:// deep link.
 *
 * The data injection
 *   window.__KAL_GRAPH__   the contents of kal-graph.json (inlined by build_web_viewer.py)
 *   window.__KAL_VAULT__   the vault name — needed by the obsidian:// links
 *   Why fetch is not used: opened over file://, CORS blocks it.  Opening with one double-click is
 *   the point of this viewer, so it is built as a self-contained HTML file.
 */
import './dom';
import type { App, CachedMetadata, EventRef, TAbstractFile, TFile } from 'obsidian';
import { Component, Notice } from 'obsidian';
import { DEFAULT_SETTINGS, mergeSettings, type GalaxySettings } from '../settings';
import { GraphController } from '../view/GraphController';
import { KDB_PREFIX, KDB_JSON_PATH } from '../data/kdbGraph';

declare global {
	interface Window {
		__KAL_GRAPH__?: unknown;
		__KAL_VAULT__?: string;
		galaxy?: { controller: GraphController; settings: GalaxySettings };
	}
}

const SETTINGS_KEY = 'galaxy-view-web:settings';
const FILES_KEY = 'galaxy-view-web:files';
const DEAD: EventRef = { off() {} };

/* ── the vault stand-in ────────────────────────────────────────────── */

/** Every adapter path the plugin uses is sent to localStorage (the coordinate cache and so on) */
function makeAdapter(graphJson: string) {
	const files: Record<string, string> = JSON.parse(localStorage.getItem(FILES_KEY) ?? '{}');
	const isGraph = (p: string) => p.endsWith(KDB_JSON_PATH) || p.endsWith('kal-graph.json');
	const flush = () => {
		try {
			localStorage.setItem(FILES_KEY, JSON.stringify(files));
		} catch {
			/* Over quota — the coordinate cache is optional, so it passes quietly */
		}
	};
	return {
		async exists(p: string) {
			return isGraph(p) || p in files;
		},
		async read(p: string) {
			return isGraph(p) ? graphJson : (files[p] ?? '');
		},
		async write(p: string, data: string) {
			files[p] = data;
			flush();
		},
		async mkdir() {},
	};
}

function makeApp(graphJson: string, vaultName: string): App {
	const uri = (path: string) =>
		`obsidian://open?vault=${encodeURIComponent(vaultName)}&file=${encodeURIComponent(path.replace(/\.md$/, ''))}`;

	return {
		vault: {
			configDir: '.obsidian',
			adapter: makeAdapter(graphJson),
			getFiles: () => [] as TFile[],
			getAbstractFileByPath: () => null as TAbstractFile | null,
			cachedRead: async () => '',
			on: () => DEAD,
		},
		metadataCache: {
			resolvedLinks: {},
			unresolvedLinks: {},
			getFileCache: () => null as CachedMetadata | null,
			on: () => DEAD,
		},
		workspace: {
			openLinkText: (link: string) => {
				// An entity node's own id (`kdb:37`) is not something to open — it is a concept, not a document.
				// Only the card's 'source document' paths lead to a real note.
				if (!link || link.startsWith(KDB_PREFIX)) {
					new Notice('This node is an entity — click the source document path on the card');
					return;
				}
				window.location.href = uri(link);
			},
			on: () => DEAD,
			getLeavesOfType: () => [],
			detachLeavesOfType: () => {},
		},
	};
}

/* ── boot ──────────────────────────────────────────────────────────── */

function loadSettings(): GalaxySettings {
	let stored: unknown = null;
	try {
		stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? 'null');
	} catch {
		/* Corrupted settings fall back to the defaults */
	}
	const s = mergeSettings({ ...DEFAULT_SETTINGS, ...(stored as object | null) });
	s.kdbMode = true; // the reason this viewer exists.  It does not start on the note-link graph
	return s;
}

async function boot(): Promise<void> {
	const graph = window.__KAL_GRAPH__;
	const host = document.getElementById('galaxy') as HTMLElement | null;
	if (!host) throw new Error('#galaxy missing');
	if (!graph) {
		host.textContent = 'kal-graph.json is not inlined. Rebuild with build_web_viewer.py.';
		return;
	}

	host.classList.add('galaxy-view-content');
	const settings = loadSettings();
	const app = makeApp(JSON.stringify(graph), window.__KAL_VAULT__ ?? 'vault');

	const save = () => {
		try {
			localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
		} catch {
			/* A failed save is no reason to block the screen */
		}
	};

	const controller = new GraphController(app, host, settings, save);
	// The same as what GalaxyView did — attaching the store as a child hands it the event cleanup
	const root = new Component();
	root.addChild(controller.store);
	root.load();

	await controller.start();
	window.addEventListener('resize', () => controller.resize());
	// A handle for development and debugging.  Reachable from the console as galaxy.controller
	window.galaxy = { controller, settings };

	document.getElementById('boot')?.remove();
}

void boot().catch((e: unknown) => {
	const host = document.getElementById('galaxy');
	if (host) host.textContent = `Failed to start: ${String(e)}`;
	console.error(e);
});
