import type { GraphData } from '../types';
import type { Adjacency } from './Adjacency';

export interface TopTag {
	id: string;
	name: string;
	count: number;
}

export interface TagLensFocus {
	/** null when the matching hub is off or outside the top N; the Lens itself does not depend on a hub existing. */
	tagIndex: number | null;
	nodeIndices: Set<number>;
	linkIndices: number[];
}

export interface ResolvedTagLens {
	id: string | null;
	focus: TagLensFocus | null;
}

export const TAG_HUB_LIMIT_MIN = 5;
export const TAG_HUB_LIMIT_MAX = 50;

export function boundedTagHubLimit(value: number): number {
	if (!Number.isFinite(value)) return 20;
	return Math.min(Math.max(Math.round(value), TAG_HUB_LIMIT_MIN), TAG_HUB_LIMIT_MAX);
}

/** The first tag a note carries in document order; the colouring uses this one alone, avoiding the unclear semantics of blending several tags. */
export function primaryTag(node: GraphData['nodes'][number]): string | null {
	return node.tag ? (node.tags[0] ?? node.name) : (node.tags[0] ?? null);
}

/** The tags in the current graph, descending by note count; it does not depend on hub nodes, and a tag activated from outside the list keeps its clear-it entry point. */
export function topTags(data: GraphData, limit = 12, activeId: string | null = null): TopTag[] {
	if (limit <= 0) return [];
	const counts = new Map<string, number>();
	for (const node of data.nodes) {
		if (node.tag || node.unresolved) continue;
		for (const tag of new Set(node.tags)) counts.set(tag, (counts.get(tag) ?? 0) + 1);
	}
	const ranked = [...counts.entries()]
		.sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
		.map(([name, count]) => ({ id: `tag:${name}`, name, count }));
	const visible = ranked.slice(0, limit);
	if (activeId && !visible.some((tag) => tag.id === activeId)) {
		const active = ranked.find((tag) => tag.id === activeId);
		if (active) visible.push(active);
	}
	return visible;
}

/** A single-select chip: clicking the current tag again clears it. */
export function toggleTagLens(current: string | null, clicked: string): string | null {
	return current === clicked ? null : clicked;
}

/**
 * Resolve the Lens from the note's own tags; a hub is only an optional visual, not a data dependency of the Lens.
 * Computed once on click in O(N+E) and cached into the render buffer, so no animation frame scans the graph.
 */
export function tagLensFocus(data: GraphData, adjacency: Adjacency, tagId: string | null): TagLensFocus | null {
	if (!tagId) return null;
	const tagName = tagId.startsWith('tag:') ? tagId.slice(4) : tagId;
	const nodeIndices = new Set<number>();
	let tagIndex: number | null = null;
	for (let i = 0; i < data.nodes.length; i++) {
		const node = data.nodes[i];
		if (!node) continue;
		if (node.tag && node.id === `tag:${tagName}`) {
			tagIndex = i;
			nodeIndices.add(i);
		} else if (!node.tag && !node.unresolved && node.tags.includes(tagName)) {
			nodeIndices.add(i);
		}
	}
	if (nodeIndices.size === 0 || (tagIndex !== null && nodeIndices.size === 1)) return null;

	const linkSet = new Set<number>();
	if (tagIndex !== null) {
		const start = adjacency.offset[tagIndex] ?? 0;
		const end = adjacency.offset[tagIndex + 1] ?? start;
		for (let edge = start; edge < end; edge++) linkSet.add(adjacency.linkOf[edge] ?? 0);
	}
	for (let i = 0; i < data.links.length; i++) {
		const link = data.links[i];
		if (link && nodeIndices.has(link.source) && nodeIndices.has(link.target)) linkSet.add(i);
	}
	const linkIndices = [...linkSet].sort((a, b) => a - b);
	return { tagIndex, nodeIndices, linkIndices };
}

/** showTags and the current graph together decide whether a persisted Lens is still valid. */
export function resolveTagLens(data: GraphData, adjacency: Adjacency, showTags: boolean, requested: string | null): ResolvedTagLens {
	if (!showTags || !requested) return { id: null, focus: null };
	const focus = tagLensFocus(data, adjacency, requested);
	return focus ? { id: requested, focus } : { id: null, focus: null };
}
