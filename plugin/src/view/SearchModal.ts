import type { App } from 'obsidian';
import { SuggestModal, prepareFuzzySearch } from 'obsidian';
import type { GraphNode } from '../types';
import { t } from '../i18n';

interface Hit {
	index: number;
	node: GraphNode;
	score: number;
}

/** Search the galaxy's nodes → fly the camera (Obsidian's native SuggestModal, keyboard-friendly) */
export class NodeSearchModal extends SuggestModal<Hit> {
	constructor(
		app: App,
		private nodes: GraphNode[],
		private onPick: (index: number) => void,
		placeholder?: string,
	) {
		super(app);
		const label = placeholder ?? t('search.placeholder');
		this.setPlaceholder(label);
		/* The dialog semantics are given here — because Obsidian does not attach them either.
		   (obsidian.asar/app.js, measured 2026-08-21: 0 aria-modal, 0 role="dialog",
		   modal-container is a bare div.)  The web shim is a <dialog> and has them implicitly, but
		   stating them does no harm, so both sides are handled in one place.
		   The name reuses the placeholder already translated into 6 languages rather than adding a new i18n key.
		   The placeholder disappears once typing starts, so it is set on the input separately. */
		this.containerEl.setAttribute('role', 'dialog');
		this.containerEl.setAttribute('aria-modal', 'true');
		this.containerEl.setAttribute('aria-label', label);
		this.inputEl.setAttribute('aria-label', label);
	}

	getSuggestions(query: string): Hit[] {
		const q = query.trim();
		if (!q) {
			// An empty query: the top 20 hubs by degree —— a "constellation tour"
			return [...this.nodes.entries()]
				.filter(([, n]) => !n.unresolved && !n.tag)
				.sort((a, b) => b[1].degree - a[1].degree)
				.slice(0, 20)
				.map(([index, node]) => ({ index, node, score: 0 }));
		}
		const fuzzy = prepareFuzzySearch(q);
		const hits: Hit[] = [];
		for (let i = 0; i < this.nodes.length; i++) {
			const node = this.nodes[i];
			if (!node) continue;
			const m = fuzzy(node.name) ?? fuzzy(node.id);
			if (m) hits.push({ index: i, node, score: m.score });
		}
		return hits.sort((a, b) => b.score - a.score || b.node.degree - a.node.degree).slice(0, 50);
	}

	renderSuggestion(hit: Hit, el: HTMLElement): void {
		el.createDiv({ text: hit.node.name });
		el.createDiv({
			cls: 'gx-search-path',
			text: t('search.hit', { path: hit.node.unresolved ? t('search.unresolved') : hit.node.id, n: hit.node.degree }),
		});
	}

	onChooseSuggestion(hit: Hit): void {
		this.onPick(hit.index);
	}
}
