export interface GraphNode {
	id: string; // the vault path; "unresolved:<name>" when unresolved; "tag:#<name>" for a tag
	name: string;
	/**
	 * The **group name** the node belongs to.  It is called 'folder', but one of three things arrives:
	 *   the note graph      the top-level folder (root is '' · unresolved '__unresolved__' · tags '__tag__')
	 *   entities, by type   'concept' 'artifact' 'method' …
	 *   entities, by community  the Louvain community name ('hybrid search system' and the like)
	 * The colour groups, the legend and the filter all read this one field —— which is why the name was left alone.
	 * Changing it means moving palette, noteFilter and ControlPanel with it.
	 */
	folderTop: string;
	degree: number; // out + in
	inDegree: number;
	outDegree: number;
	fileSize: number; // bytes; 0 for unresolved and tag nodes (an optional basis for "mass")
	/** The note's metadata tags; a tag hub itself holds only the one tag it stands for. */
	tags: string[];
	unresolved: boolean;
	tag: boolean; // true = a bounded tag hub (not a file), a discriminator alongside unresolved
}

/** An edge is expressed as node-array indices —— the aggregate renderer gathers coordinates by index */
export interface GraphLink {
	source: number;
	target: number;
}

export interface GraphData {
	nodes: GraphNode[];
	links: GraphLink[];
}

export interface LayoutParams {
	charge: number; // negative = repulsion
	linkDistance: number;
	linkStrength: number; // a multiplier: 1 = the d3 default (1/min(endpoint degree))
	centerPull: number; // the forceX/Y/Z strength, stopping orphans flying off
	flatten: number; // 0 = a natural sphere; >0 adds pressure on the Y axis → a galactic disc (natural attraction and repulsion cannot make a disc, so this extra force is necessary)
	coreGravity: number; // radial core gravity: a dense bright core + a radial density gradient (degree-weighted, hubs sinking to the core)
	spiral: number; // the tangential spiral-arm force: combs the disc into logarithmic arms (0 = no arms)
	/** Community cohesion.  Pulls nodes sharing a folderTop towards **the current centre of mass**.
	 *  It is a force that tightens a clump already separated —— it cannot separate one. */
	community: number;
	velocityDecay: number;
}

export interface FrameStats {
	frames: number;
	avgFps: number;
	p95FrameMs: number;
	worstFrameMs: number;
	durationMs: number;
}

export interface BenchResult {
	scenario: string;
	timestamp: string;
	nodes: number;
	links: number;
	bloom: boolean;
	[key: string]: unknown;
}
